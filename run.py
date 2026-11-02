import asyncio
import io

from flask import jsonify, send_file
from app.services.stt import transcribe_audio
from app.services.tts import synthesize_speech

from flask import Flask, render_template, request, session, redirect, url_for
from app.services.gemini import (
    generate_interview_plan,
    evaluate_answer,
    generate_final_scorecard
)
from app.services.resume_parser import extract_resume_text


app = Flask(
    __name__,
    template_folder="app/templates",
    static_folder="app/static"
)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

app.secret_key = "dev-secret-key"


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

        session["candidate_name"] = candidate_name
        session["interview_plan"] = interview_plan
        session["current_question"] = 0
        session["interview_history"] = []

        return redirect(url_for("start_interview"))

    return render_template("index.html")


@app.route("/start-interview")
def start_interview():

    interview_plan = session.get("interview_plan")

    if not interview_plan:
        return redirect(url_for("home"))

    questions = interview_plan["questions"]
    current_question = session.get("current_question", 0)

    if current_question >= len(questions):
        return "Interview completed!"

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

    answer = request.form.get("answer")

    interview_plan = session.get("interview_plan")

    if not interview_plan:
        return redirect(url_for("home"))

    current_question = session.get("current_question", 0)

    question = interview_plan["questions"][current_question]

    evaluation = evaluate_answer(
        question,
        answer
    )
    history = session.get("interview_history", [])

    history.append({
    "question": question["question"],
    "skill": question["skill"],
    "type": question["type"],
    "answer": answer,
    "evaluation": evaluation
    })

    session["interview_history"] = history

    print("Candidate Answer:")
    print(answer)

    print("Evaluation:")
    print(evaluation)

    if evaluation["follow_up_needed"]:

        follow_up_question = {
            "type": "follow_up",
            "skill": question["skill"],
            "question": evaluation["follow_up_question"]
        }

        session["current_follow_up"] = follow_up_question

        return redirect(url_for("follow_up"))

    session["current_question"] = current_question + 1

    if session["current_question"] >= len(interview_plan["questions"]):
        return redirect(url_for("final_scorecard"))

    return redirect(url_for("start_interview"))

@app.route("/follow-up")
def follow_up():

    follow_up_question = session.get("current_follow_up")

    if not follow_up_question:
        return redirect(url_for("start_interview"))

    return render_template(
        "interview.html",
        candidate_name=session.get("candidate_name"),
        question=follow_up_question,
        question_number="Follow-up",
        total_questions=""
    )
@app.route("/submit-follow-up", methods=["POST"])
def submit_follow_up():

    answer = request.form.get("answer")

    follow_up_question = session.get("current_follow_up")

    history = session.get("interview_history", [])

    if history:

        history[-1]["follow_up"] = {
            "question": follow_up_question["question"],
            "answer": answer
        }

    session["interview_history"] = history

    print("Follow-up Question:")
    print(follow_up_question["question"])

    print("Follow-up Answer:")
    print(answer)

    session.pop("current_follow_up", None)

    current_question = session.get("current_question", 0)

    session["current_question"] = current_question + 1

    interview_plan = session.get("interview_plan")

    if session["current_question"] >= len(interview_plan["questions"]):
        return redirect(url_for("final_scorecard"))

    return redirect(url_for("start_interview"))

@app.route("/final-scorecard")
def final_scorecard():

    interview_history = session.get("interview_history", [])

    if not interview_history:
        return redirect(url_for("home"))

    scorecard = generate_final_scorecard(
        session.get("candidate_name"),
        interview_history
    )

    return render_template(
        "scorecard.html",
        candidate_name=session.get("candidate_name"),
        scorecard=scorecard
    )


@app.route("/api/transcribe", methods=["POST"])
def transcribe():

    audio_file = request.files.get("audio")

    try:
        result = transcribe_audio(audio_file)
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
        audio = asyncio.run(synthesize_speech(text))

        return send_file(
            io.BytesIO(audio),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="question.mp3"
        )

    except Exception:
        app.logger.exception("Speech synthesis failed")
        return jsonify({
            "error": "Speech synthesis failed."
        }), 502


if __name__ == "__main__":
    app.run(debug=True)