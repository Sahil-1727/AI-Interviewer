import asyncio
import io
import os
import time
from pathlib import Path
from uuid import uuid4

from cachelib.file import FileSystemCache
from flask import jsonify, send_file
from flask_session import Session
from app.services.stt import transcribe_audio
from app.services.tts import synthesize_speech

from flask import Flask, render_template, request, session, redirect, url_for
from app.services.gemini import (
    generate_interview_plan,
    evaluate_answer,
    generate_final_scorecard
)
from app.services.resume_parser import extract_resume_text
from app.voice_config import voice_config
from app.services.conversation import ConversationService, ConversationError
from app.services.interview_flow import advance, copy_to_session
from app.routes.conversation import conversation_bp


app = Flask(
    __name__,
    template_folder="app/templates",
    static_folder="app/static"
)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")


@app.errorhandler(413)
def upload_too_large(error):
    return jsonify(error="Recording or upload exceeds the 12 MB limit. Use shorter recording segments."), 413

# Keep growing interview plans and history on the server. The browser cookie
# contains only a session ID, so submitting later answers cannot exceed 4 KB.
session_directory = Path(os.getenv("INTERVIEW_SESSION_DIR", str(Path(app.instance_path) / "sessions")))
session_directory.mkdir(parents=True, exist_ok=True)
app.config.update(
    SESSION_TYPE="cachelib",
    SESSION_CACHELIB=FileSystemCache(cache_dir=str(session_directory)),
    SESSION_PERMANENT=False,
)
Session(app)
voice_journal = None
if os.getenv("DATABASE_URL"):
    from app.database import db, init_database
    from app.services.postgres_voice import PostgresVoiceJournal
    init_database(app)
    with app.app_context():
        voice_journal = PostgresVoiceJournal(db.engine)
app.extensions["conversation"] = ConversationService(
    os.getenv("CONVERSATION_STATE_DIR", str(Path(app.instance_path) / "conversations")),
    lambda question, answer: evaluate_answer(question, answer),
    lambda candidate, history: generate_final_scorecard(candidate, history),
    voice_config(), journal=voice_journal,
)
app.register_blueprint(conversation_bp)


def sync_voice_session(manual=False):
    identity = session.get("voice_interview_id")
    if identity:
        service = app.extensions["conversation"]
        service.authorize(identity, session)
        state = service.control(identity, "manual") if manual else service.get(identity)
        copy_to_session(state, session)
        if manual:
            session.pop("voice_interview_id", None)
            session.pop("voice_interview_token", None)
        return state


@app.errorhandler(ConversationError)
def conversation_error(error):
    return jsonify(error=str(error), recoverable=True), error.status


@app.context_processor
def experimental_voice_settings():
    return {"voice_config": voice_config(), "voice_mock_mode": app.config.get("VOICE_MOCK_MODE", False)}


@app.route("/", methods=["GET", "POST"])
def home():

    if request.method == "POST":

        candidate_name = request.form.get("candidate_name")
        job_description = request.form.get("job_description")
        resume_file = request.files.get("resume")
        try:

            resume = extract_resume_text(resume_file)

        except ValueError as error:

            return f"Resume Error: {error}", 400

        interview_plan = generate_interview_plan(
            candidate_name,
            job_description,
            resume
        )

        session.clear()
        session["candidate_name"] = candidate_name
        session["evaluation_context"] = {"job_description": job_description, "resume": resume}
        session["interview_plan"] = interview_plan
        session["current_question"] = 0
        session["interview_history"] = []

        return redirect(url_for("start_interview"))

    return render_template("index.html")


@app.route("/start-interview")
def start_interview():

    voice_state = sync_voice_session()
    interview_plan = session.get("interview_plan")

    if not interview_plan:
        return redirect(url_for("home"))

    session.setdefault("interview_instance_id", uuid4().hex)

    questions = interview_plan["questions"]
    current_question = session.get("current_question", 0)

    if current_question >= len(questions):
        if voice_state and voice_state["status"] != "completed":
            # A failed/in-flight scorecard must remain recoverable after refresh.
            return render_template("interview.html", candidate_name=session.get("candidate_name"),
                                   question=questions[-1], question_number=len(questions), total_questions=len(questions))
        return redirect(url_for("final_scorecard"))

    question = questions[current_question]

    return render_template(
        "interview.html",
        candidate_name=session.get("candidate_name"),
        question=question,
        question_number=current_question + 1,
        total_questions=len(questions)
    )

@app.route("/submit-answer", methods=["POST"])
def submit_answer():

    if session.get("voice_interview_id"):
        return "Switch to manual mode before using the answer form.", 409
    answer = request.form.get("answer")

    interview_plan = session.get("interview_plan")

    if not interview_plan:
        return redirect(url_for("home"))

    current_question = session.get("current_question", 0)

    if current_question >= len(interview_plan["questions"]):
        return redirect(url_for("final_scorecard"))

    if session.get("current_follow_up"):
        return redirect(url_for("follow_up"))

    sync_voice_session(manual=True)
    try:
        next_question = advance(session, answer, evaluate_answer)
    except ValueError as error:
        return str(error), 400
    if session.get("current_follow_up"):
        return redirect(url_for("follow_up"))
    return redirect(url_for("start_interview" if next_question else "final_scorecard"))

@app.route("/follow-up")
def follow_up():

    sync_voice_session()
    follow_up_question = session.get("current_follow_up")

    if not follow_up_question:
        return redirect(url_for("start_interview"))

    session.setdefault("interview_instance_id", uuid4().hex)

    return render_template(
        "interview.html",
        candidate_name=session.get("candidate_name"),
        question=follow_up_question,
        question_number="Follow-up",
        total_questions=""
    )
@app.route("/submit-follow-up", methods=["POST"])
def submit_follow_up():

    if session.get("voice_interview_id"):
        return "Switch to manual mode before using the answer form.", 409
    answer = request.form.get("answer")

    follow_up_question = session.get("current_follow_up")

    if not follow_up_question:
        return redirect(url_for("start_interview"))

    sync_voice_session(manual=True)
    try:
        next_question = advance(session, answer, evaluate_answer)
    except ValueError as error:
        return str(error), 400
    return redirect(url_for("start_interview" if next_question else "final_scorecard"))


@app.route("/final-scorecard")
def final_scorecard():

    state = sync_voice_session()
    if state and state["status"] != "completed":
        return redirect(url_for("start_interview"))
    interview_history = session.get("interview_history", [])

    early_ended = bool(state and state.get("early_ended"))
    if not interview_history and not early_ended:
        return redirect(url_for("home"))

    scorecard = session.get("final_scorecard")
    if not scorecard:
        evaluated = [item for item in interview_history if item.get("evaluation")]
        scorecard = generate_final_scorecard(session.get("candidate_name"), evaluated) if evaluated else {"insufficient_evidence": True}
        session["final_scorecard"] = scorecard

    return render_template(
        "scorecard.html",
        candidate_name=session.get("candidate_name"),
        scorecard=scorecard,
        early_ended=early_ended,
        answered_questions=sum(bool(item.get("evaluation")) for item in interview_history),
        unanswered_questions=[entry for item in interview_history
                              for entry in (item, item.get("follow_up", {}))
                              if entry.get("status") == "Unanswered"],
        planned_questions=len(session.get("interview_plan", {}).get("questions", [])),
    )


@app.route("/api/transcribe", methods=["POST"])
def transcribe():

    audio_file = request.files.get("audio")
    started = time.perf_counter()

    try:
        if request.form.get("detect_completion") == "true":
            result = transcribe_audio(audio_file, detect_completion=True)
        else:
            result = transcribe_audio(audio_file)
        elapsed = (time.perf_counter() - started) * 1000
        result["transcription_ms"] = round(elapsed, 2)
        app.logger.info("voice_timing stage=transcription ms=%.2f", elapsed)
        return jsonify(result), 200

    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    except Exception:
        app.logger.exception("Speech transcription failed")
        return jsonify({
            "error": "Audio transcription failed."
        }), 500


@app.route("/api/synthesize", methods=["POST"])
def synthesize():

    data = request.get_json(silent=True) or {}
    text = data.get("text", "")

    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "Text is required."}), 400

    if len(text) > 2000:
        return jsonify({"error": "Text is too long."}), 400

    try:
        started = time.perf_counter()
        audio = asyncio.run(asyncio.wait_for(synthesize_speech(text), timeout=voice_config()["TTS_TIMEOUT_SECONDS"]))
        elapsed = (time.perf_counter() - started) * 1000
        app.logger.info("voice_timing stage=tts ms=%.2f", elapsed)
        response = send_file(
            io.BytesIO(audio),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="question.mp3"
        )
        response.headers["Server-Timing"] = f"tts;dur={elapsed:.2f}"
        response.headers["Cache-Control"] = "no-store"
        return response

    except Exception:
        app.logger.exception("Speech synthesis failed")
        return jsonify({
            "error": "Speech synthesis failed."
        }), 502


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", threaded=True)
