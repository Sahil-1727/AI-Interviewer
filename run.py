import asyncio
import io
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from cachelib.file import FileSystemCache
from flask import g, jsonify, send_file
from flask_session import Session
from sqlalchemy.exc import SQLAlchemyError
from google.genai.errors import APIError as GeminiAPIError
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
from app.routes.accounts import accounts_bp, daily_usage, protect_accounts
from app.services.interview_repository import InterviewQuotaExceeded


app = Flask(
    __name__,
    template_folder="app/templates",
    static_folder="app/static"
)
# Only trust forwarding headers when the host's proxy boundary is configured.
proxy_hops = int(os.getenv('TRUSTED_PROXY_HOPS', '0'))
if proxy_hops < 0:
    raise RuntimeError('TRUSTED_PROXY_HOPS must be nonnegative.')
if proxy_hops:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops,
                            x_host=0, x_port=0, x_prefix=0)
# Import LiveKit's token SDK during startup so the first candidate click does
# not pay its import cost. The agent itself runs in the separate worker.
livekit_enabled = os.getenv('LIVEKIT_ENABLED', '0').strip() == '1'
if livekit_enabled:
    from livekit.api import AccessToken as _LiveKitAccessToken  # noqa: F401
from app.request_timing import RequestTiming
app.wsgi_app = RequestTiming(app.wsgi_app)
app.jinja_env.globals['livekit_enabled'] = livekit_enabled
app.jinja_env.globals['livekit_primary'] = livekit_enabled
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv('MAX_REQUEST_BYTES', str(12 * 1024 * 1024)))
app.config['SOCK_SERVER_OPTIONS'] = {'max_message_size': 256 * 1024, 'ping_interval': 25}
READINESS_TABLES = (
    'web_sessions', 'users', 'interviews', 'questions', 'answers',
    'evaluations', 'conversation_messages', 'scorecards', 'interview_requests',
    'interview_access', 'voice_snapshots', 'request_limits', 'daily_interview_usage',
)
if app.config['MAX_CONTENT_LENGTH'] <= 0:
    raise RuntimeError('MAX_REQUEST_BYTES must be positive.')

if os.getenv('APP_ENV') == 'production':
    if not os.getenv('DATABASE_URL') or os.getenv('SESSION_COOKIE_SECURE') != '1':
        raise RuntimeError('Production requires DATABASE_URL and SESSION_COOKIE_SECURE=1.')
    if len(os.getenv('SECRET_KEY', '')) < 32:
        raise RuntimeError('Production requires a SECRET_KEY of at least 32 characters.')
    if os.getenv('FLASK_DEBUG') == '1':
        raise RuntimeError('Production cannot run in debug mode.')
    if os.getenv('CSRF_ENABLED', '1') == '0' or os.getenv('RATE_LIMIT_ENABLED', '1') == '0':
        raise RuntimeError('Production requires CSRF protection and rate limiting.')
    if livekit_enabled:
        required_livekit = ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET')
        missing_livekit = [name for name in required_livekit if not os.getenv(name, '').strip()]
        if (missing_livekit or len(os.getenv('LIVEKIT_AGENT_SHARED_SECRET', '').strip()) < 32
                or not os.getenv('LIVEKIT_URL', '').startswith('wss://')):
            raise RuntimeError('LIVEKIT_ENABLED=1 requires LiveKit URL/key/secret and a 32-character worker shared secret.')

if os.getenv("DATABASE_URL") and not os.getenv("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY must be set when using PostgreSQL-backed sessions.")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")


@app.errorhandler(413)
def upload_too_large(error):
    return jsonify(error="Recording or upload exceeds the 12 MB limit. Use shorter recording segments."), 413


@app.errorhandler(SQLAlchemyError)
def database_unavailable(error):
    sqlstate = getattr(getattr(error, 'orig', None), 'sqlstate', None)
    app.logger.warning('Database operation failed: error_type=%s sqlstate=%s',
                       type(error).__name__, sqlstate or 'unknown')
    response = jsonify(error='Service temporarily unavailable.')
    response.status_code = 503
    response.headers['Cache-Control'] = 'no-store'
    return response

voice_journal = None
if os.getenv("DATABASE_URL"):
    from app.database import db, init_database
    from app.services.postgres_voice import PostgresVoiceJournal
    init_database(app)
    app.config.update(SESSION_TYPE="sqlalchemy", SESSION_SQLALCHEMY=db,
                      SESSION_SQLALCHEMY_TABLE="web_sessions", SESSION_PERMANENT=False,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1")
    from app.services.shared_session import SharedSessionInterface
    retention_seconds = int(os.getenv('SESSION_RETENTION_SECONDS', '86400'))
    if retention_seconds <= 0:
        raise RuntimeError('SESSION_RETENTION_SECONDS must be positive.')
    app.permanent_session_lifetime = timedelta(seconds=retention_seconds)
    app.session_interface = SharedSessionInterface(app, client=db, table='web_sessions', permanent=False)
    with app.app_context():
        voice_journal = PostgresVoiceJournal(db.engine)
else:
    session_directory = Path(os.getenv("INTERVIEW_SESSION_DIR", str(Path(app.instance_path) / "sessions")))
    session_directory.mkdir(parents=True, exist_ok=True)
    app.config.update(SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(cache_dir=str(session_directory)), SESSION_PERMANENT=False)
    Session(app)
app.extensions["conversation"] = ConversationService(
    os.getenv("CONVERSATION_STATE_DIR", str(Path(app.instance_path) / "conversations")),
    lambda question, answer: evaluate_answer(question, answer),
    lambda candidate, history: generate_final_scorecard(candidate, history),
    voice_config(), journal=voice_journal,
)
app.register_blueprint(conversation_bp)
app.register_blueprint(accounts_bp)
from app.routes.history import history_bp
app.register_blueprint(history_bp)
from app.routes.deepgram_stream import sock
sock.init_app(app)


@app.before_request
def check_session_storage():
    if livekit_enabled and os.getenv('APP_ENV') == 'production' and request.path in {
        '/api/transcribe', '/api/synthesize', '/api/voice/stream'
    }:
        return jsonify(error='This voice endpoint is disabled for the LiveKit interview transport.'), 404
    if getattr(session, '_storage_unavailable', False):
        return jsonify(error='Service temporarily unavailable.'), 503


from app.security import init_security
init_security(app)
protect_accounts(app)
from app.capacity import init_capacity
init_capacity(app)


@app.get('/health/ready')
def readiness():
    if not os.getenv('DATABASE_URL'):
        response = jsonify(status='unavailable')
        response.headers['Cache-Control'] = 'no-store'
        return response, 503
    try:
        from sqlalchemy import text
        table_names = ', '.join(f"'{name}'" for name in READINESS_TABLES)
        with db.engine.connect() as connection:
            connection.execute(text('SELECT 1'))
            # A reachable PostgreSQL server is not sufficient if migrations
            # have not created the tables needed by sessions and interviews.
            rows = connection.execute(text(f"""
                SELECT name,
                       to_regclass(format('%I.%I', current_schema(), name)) IS NOT NULL AS present
                FROM unnest(ARRAY[{table_names}]) AS required(name)
            """)).all()
        if len(rows) != len(READINESS_TABLES) or any(not row.present for row in rows):
            response = jsonify(status='unavailable')
            response.headers['Cache-Control'] = 'no-store'
            return response, 503
        response = jsonify(status='ready')
        response.headers['Cache-Control'] = 'no-store'
        return response, 200
    except Exception:
        response = jsonify(status='unavailable')
        response.headers['Cache-Control'] = 'no-store'
        return response, 503


@app.cli.command('cleanup-expired-sessions')
def cleanup_expired_sessions():
    """Remove expired browser sessions and old rate buckets; never interviews."""
    import click
    from sqlalchemy import text
    if not os.getenv('DATABASE_URL'):
        raise click.ClickException('DATABASE_URL is required.')
    with db.engine.begin() as connection:
        removed = connection.execute(text("DELETE FROM web_sessions WHERE expiry < (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')")).rowcount
        connection.execute(text('DELETE FROM request_limits WHERE bucket < :cutoff'), {'cutoff': int(time.time()) // 60 - 1440})
    click.echo(f'Removed {removed} expired browser sessions.')


@app.get('/api/conversation/transcript')
def voice_transcript():
    identity = session.get('voice_interview_id') or session.get('manual_interview_id')
    token = session.get('voice_interview_token') or session.get('manual_interview_token')
    journal = app.extensions['conversation'].journal
    if not identity or not hasattr(journal, 'sessions'):
        return jsonify(error='Interview not found.'), 404
    from app.services.interview_repository import InterviewNotFound, export_transcript
    try:
        journal.authorize(identity, token, session.get('account_id'))
        with journal.sessions.begin() as db_session:
            messages = export_transcript(db_session, identity, token)
    except InterviewNotFound:
        return jsonify(error='Interview not found.'), 404
    response = jsonify(interview_id=identity, messages=messages)
    response.headers['Cache-Control'] = 'no-store'
    return response


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


def sync_manual_interview():
    identity = session.get('manual_interview_id')
    if not identity:
        return None
    service = app.extensions['conversation']
    service.journal.authorize(identity, session.get('manual_interview_token'), session.get('account_id'))
    state = service.get(identity)
    copy_to_session(state, session)
    return state


def submit_postgres_manual(answer):
    service = app.extensions['conversation']
    state = sync_manual_interview()
    if not state:
        return 'Interview session expired. Start a new interview.', 410
    turn = request.form.get('turn_id')
    if not turn:
        return 'Reload the interview before submitting your answer.', 400
    retry = state['status'] == 'error'
    if retry and (not isinstance(answer, str) or answer.strip() != state.get('draft', '').strip()):
        return 'Processing was interrupted. Retry with the saved answer shown on the page.', 409
    if retry and state.get('paused'):
        service.control(state['id'], 'resume')
    if state['status'] == 'manual':
        with service.journal.edit(state['id']) as stored:
            if stored['status'] == 'manual':
                stored.update(status='ready', paused=False)
    payload = dict(turn_id=turn, request_id=uuid4().hex if retry else turn, answer=answer)
    try:
        state = service.accept(state['id'], payload, retry=retry)
        deadline = time.monotonic() + voice_config()['REQUEST_TIMEOUT_SECONDS']
        while state['status'] in {'evaluating', 'scoring', 'ending'} and time.monotonic() < deadline:
            time.sleep(.1)
            state = service.get(state['id'])
        copy_to_session(state, session)
        if state['status'] == 'error':
            return 'Processing was interrupted. Refresh the interview to retry the saved answer.', 503
        if state['status'] in {'evaluating', 'scoring', 'ending'}:
            return 'Your answer is processing. Refresh the interview to continue.', 503
        if state.get('current_follow_up'):
            return redirect(url_for('follow_up'))
        return redirect(url_for('final_scorecard' if state['status'] == 'completed' else 'start_interview'))
    except ConversationError as error:
        return error.args[0], error.status


@app.errorhandler(ConversationError)
def conversation_error(error):
    return jsonify(error=str(error), recoverable=True), error.status


@app.context_processor
def experimental_voice_settings():
    return {"voice_config": {**voice_config(), "STT_PROVIDER": os.getenv('STT_PROVIDER', 'whisper').strip().lower()}, "voice_mock_mode": app.config.get("VOICE_MOCK_MODE", False)}


@app.route("/", methods=["GET", "POST"])
def home():

    if request.method == "POST":

        used_today, daily_limit = daily_usage(g.current_account.id)
        if used_today >= daily_limit:
            return render_template('index.html', error='You have reached your interview limit for today.',
                                   candidate_name=g.current_account.display_name,
                                   interviews_used=used_today, daily_limit=daily_limit), 429

        candidate_name = request.form.get("candidate_name")
        job_description = request.form.get("job_description")
        resume_file = request.files.get("resume")
        if not candidate_name or not candidate_name.strip() or len(candidate_name) > 255:
            return 'Provide a candidate name of at most 255 characters.', 400
        if not job_description or not job_description.strip() or len(job_description) > 50000:
            return 'Provide a job description of at most 50,000 characters.', 400
        try:

            resume = extract_resume_text(resume_file)

        except ValueError as error:

            return f"Resume Error: {error}", 400

        if not isinstance(resume, str) or not resume.strip() or len(resume) > 100000:
            return 'Provide a readable resume with at most 100,000 characters of text.', 400

        try:
            interview_plan = generate_interview_plan(
                candidate_name,
                job_description,
                resume
            )
        except GeminiAPIError as error:
            provider_status = getattr(error, 'code', None)
            app.logger.warning('Interview plan generation failed: error_type=%s provider_status=%s',
                               type(error).__name__, provider_status or 'unknown')
            temporary = provider_status == 429 or (isinstance(provider_status, int) and provider_status >= 500)
            message = ('Gemini is temporarily busy. Your interview was not created. Please try again shortly; '
                       'your name and job description are kept, but you will need to select your resume again.'
                       if temporary else
                       'The question service could not create your interview. Please try again later or contact the administrator.')
            used, limit = daily_usage(g.current_account.id)
            return render_template('index.html', error=message, candidate_name=candidate_name,
                                   job_description=job_description, interviews_used=used,
                                   daily_limit=limit), (503 if temporary else 502)

        account_id = g.current_account.id
        csrf = session.get('_csrf_token')
        session.clear()
        session['account_id'] = account_id
        if csrf:
            session['_csrf_token'] = csrf
        session["candidate_name"] = candidate_name
        session["evaluation_context"] = {"job_description": job_description, "resume": resume}
        session["interview_plan"] = interview_plan
        session["current_question"] = 0
        session["interview_history"] = []

        journal = app.extensions['conversation'].journal
        if hasattr(journal, 'initialize'):
            try:
                identity, token = journal.initialize(session, False)
            except InterviewQuotaExceeded:
                session.clear()
                session['account_id'] = account_id
                if csrf:
                    session['_csrf_token'] = csrf
                used, limit = daily_usage(account_id)
                return render_template('index.html', error='You have reached your interview limit for today.',
                                       candidate_name=candidate_name, interviews_used=used,
                                       daily_limit=limit), 429
            session['manual_interview_id'] = identity
            session['manual_interview_token'] = token

        return redirect(url_for("start_interview"))

    used, limit = daily_usage(g.current_account.id)
    return render_template("index.html", candidate_name=g.current_account.display_name,
                           interviews_used=used, daily_limit=limit)


@app.route("/start-interview")
def start_interview():

    voice_state = sync_voice_session()
    manual_state = sync_manual_interview() if not voice_state else None
    interview_plan = session.get("interview_plan")

    if not interview_plan:
        return redirect(url_for("home"))

    session.setdefault("interview_instance_id", uuid4().hex)

    questions = interview_plan["questions"]
    current_question = session.get("current_question", 0)

    if current_question >= len(questions):
        active_state = voice_state or manual_state
        if active_state and active_state["status"] != "completed":
            # A failed/in-flight scorecard must remain recoverable after refresh.
            return render_template("interview.html", candidate_name=session.get("candidate_name"),
                                   question=questions[-1], question_number=len(questions), total_questions=len(questions),
                                   manual_turn_id=manual_state['turn_id'] if manual_state else None,
                                   manual_draft=manual_state.get('draft', '') if manual_state else '')
        return redirect(url_for("final_scorecard"))

    if manual_state and manual_state.get('current_follow_up'):
        return redirect(url_for('follow_up'))

    question = questions[current_question]

    return render_template(
        "interview.html",
        candidate_name=session.get("candidate_name"),
        question=question,
        question_number=current_question + 1,
        total_questions=len(questions), manual_turn_id=manual_state['turn_id'] if manual_state else None,
        manual_draft=manual_state.get('draft', '') if manual_state and manual_state.get('status') == 'error' else ''
    )

@app.route("/submit-answer", methods=["POST"])
def submit_answer():

    if session.get("voice_interview_id"):
        return "Switch to manual mode before using the answer form.", 409
    answer = request.form.get("answer")

    if session.get('manual_interview_id'):
        return submit_postgres_manual(answer)

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
    manual_state = sync_manual_interview() if not session.get('voice_interview_id') else None
    follow_up_question = session.get("current_follow_up")

    if not follow_up_question:
        return redirect(url_for("start_interview"))

    session.setdefault("interview_instance_id", uuid4().hex)

    return render_template(
        "interview.html",
        candidate_name=session.get("candidate_name"),
        question=follow_up_question,
        question_number="Follow-up",
        total_questions="", manual_turn_id=manual_state['turn_id'] if manual_state else None,
        manual_draft=manual_state.get('draft', '') if manual_state and manual_state.get('status') == 'error' else ''
    )
@app.route("/submit-follow-up", methods=["POST"])
def submit_follow_up():

    if session.get("voice_interview_id"):
        return "Switch to manual mode before using the answer form.", 409
    answer = request.form.get("answer")

    if session.get('manual_interview_id'):
        return submit_postgres_manual(answer)

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

    state = sync_voice_session() or sync_manual_interview()
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
    if audio_file is None or not audio_file.filename:
        return jsonify({"error": "Choose an audio recording before transcribing."}), 400
    if os.getenv('STT_PROVIDER', 'whisper').strip().lower() == 'deepgram':
        return jsonify({
            "error": "Recorded-audio transcription is unavailable in Deepgram mode. Resume live streaming or type your answer."
        }), 409
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
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400
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
