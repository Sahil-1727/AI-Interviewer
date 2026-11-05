"""HTTP conversation protocol. All identities are resolved from the Flask session."""
from flask import Blueprint, current_app, jsonify, request, session
import json
import os
import time
import hmac
import re
from uuid import uuid4
from datetime import timedelta
from itsdangerous import BadSignature, URLSafeTimedSerializer
from app.services.conversation import ConversationError, public_state
from app.services.interview_flow import copy_to_session

conversation_bp = Blueprint("conversation", __name__, url_prefix="/api/conversation")


def service():
    return current_app.extensions["conversation"]


def identity():
    value = session.get("voice_interview_id")
    if not value:
        raise ConversationError("No active conversation.", 404)
    service().authorize(value, session)
    return value


@conversation_bp.errorhandler(ConversationError)
def failure(error):
    response = jsonify(error=str(error), recoverable=True)
    response.status_code = error.status
    response.headers["Cache-Control"] = "no-store"
    return response


def reply(state, status=200):
    response = jsonify(public_state(state))
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _matches_planned_question(text, questions):
    normalized = re.sub(r'[^a-z0-9 ]', '', str(text or '').casefold()).strip()
    return any(
        (candidate := re.sub(r'[^a-z0-9 ]', '', str(item.get('question') or '').casefold()).strip())
        and candidate in normalized
        for item in questions if isinstance(item, dict)
    )


@conversation_bp.post("/start")
def start():
    started = time.perf_counter()
    response = reply(service().start(session, mock=current_app.config.get("VOICE_MOCK_MODE", False)))
    current_app.logger.info("voice_timing stage=conversation_start_server_ms ms=%.2f",
                            (time.perf_counter() - started) * 1000)
    return response


@conversation_bp.post('/livekit/session')
def livekit_session():
    """Issue an opt-in room token only to the owner of an active interview."""
    started = time.perf_counter()
    if os.getenv('LIVEKIT_ENABLED', '0') != '1':
        return jsonify(error='LiveKit voice mode is not enabled.'), 404
    interview_id = identity()
    url = os.getenv('LIVEKIT_URL', '').strip()
    key = os.getenv('LIVEKIT_API_KEY', '').strip()
    secret = os.getenv('LIVEKIT_API_SECRET', '').strip()
    worker_secret = os.getenv('LIVEKIT_AGENT_SHARED_SECRET', '').strip()
    if not all((url, key, secret)) or len(worker_secret) < 32 or not url.startswith('wss://'):
        return jsonify(error='LiveKit voice mode is not configured.'), 503
    from livekit.api import AccessToken, VideoGrants, RoomConfiguration, RoomAgentDispatch
    generation = uuid4().hex
    room = 'interview-' + interview_id + '-' + generation
    with service().journal.edit(interview_id) as current:
        if current.get('status') != 'ready' or current.get('paused') or current.get('end_requested'):
            raise ConversationError('This interview is not ready for a voice session.', 409)
        current['livekit_generation'] = generation
    agent_name = os.getenv('LIVEKIT_AGENT_NAME', 'ai-interviewer')
    ticket = URLSafeTimedSerializer(worker_secret, salt='livekit-interview').dumps(
        {'interview_id': interview_id, 'room': room, 'generation': generation})
    metadata = json.dumps({'ticket': ticket}, separators=(',', ':'))
    token = (AccessToken(key, secret).with_identity('candidate-' + interview_id)
             .with_ttl(timedelta(minutes=5))
             .with_grants(VideoGrants(room_join=True, room=room, can_publish=True,
                                      can_subscribe=True, can_publish_data=True))
             .with_room_config(RoomConfiguration(agents=[RoomAgentDispatch(agent_name=agent_name, metadata=metadata)]))
             .to_jwt())
    agent_mode = os.getenv('LIVEKIT_AGENT_MODE', 'conversational').strip().lower()
    if agent_mode not in {'conversational', 'flask'}:
        raise ConversationError('LiveKit agent mode is not configured.', 503)
    response = jsonify(url=url, token=token, room=room, agent_mode=agent_mode)
    response.headers['Cache-Control'] = 'no-store'
    current_app.logger.info("voice_timing stage=livekit_session_server_ms interview=%s ms=%.2f",
                            interview_id, (time.perf_counter() - started) * 1000)
    return response


@conversation_bp.post('/livekit/agent/<action>')
def livekit_agent(action):
    """Private worker API scoped to a server-signed interview/room generation."""
    secret = os.getenv('LIVEKIT_AGENT_SHARED_SECRET', '').strip()
    supplied = request.headers.get('Authorization', '')
    if (os.getenv('LIVEKIT_ENABLED') != '1' or len(secret) < 32 or
            not hmac.compare_digest(supplied, 'Bearer ' + secret)):
        return jsonify(error='Unavailable.'), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get('ticket'), str):
        return jsonify(error='Invalid request.'), 400
    try:
        scope = URLSafeTimedSerializer(secret, salt='livekit-interview').loads(body['ticket'], max_age=7200)
        interview_id, generation = scope['interview_id'], scope['generation']
        if scope['room'] != f'interview-{interview_id}-{generation}':
            raise ValueError('Invalid ticket scope')
    except (BadSignature, KeyError, TypeError, ValueError):
        return jsonify(error='Invalid or expired agent session.'), 403

    journal = service().journal
    from app.services.postgres_voice import PostgresVoiceJournal
    if isinstance(journal, PostgresVoiceJournal):
        from app.models.interview import User, VoiceSnapshot
        with journal.sessions.begin() as db_session:
            snapshot = db_session.get(VoiceSnapshot, interview_id)
            owner_id = snapshot.state.get('owner_account') if snapshot is not None else None
            if owner_id:
                owner = db_session.get(User, owner_id)
                if owner is None or not owner.is_active:
                    return jsonify(error='Interview access is unavailable.'), 410

    if action == 'context':
        started = time.perf_counter()
        with service().journal.edit(interview_id) as current:
            if current.get('livekit_generation') != generation:
                raise ConversationError('This voice session has been replaced.', 409)
            if current.get('end_requested') or current.get('status') in {'ended', 'completed'}:
                raise ConversationError('This interview has ended.', 410)
            if current.get('livekit_events'):
                history, answered, next_main, pending = service().reconcile_livekit_transcript(current)
                is_planned_question = _matches_planned_question(
                    pending, current.get('interview_plan', {}).get('questions', []))
                context = {'interview_plan': current.get('interview_plan', {}),
                           'candidate_name': current.get('candidate_name', ''),
                           'evaluation_context': current.get('evaluation_context', {}),
                           'current_question': next_main,
                           'current_follow_up': ({'question': pending}
                                                 if pending and answered and not is_planned_question else None),
                           'interview_history': history}
            else:
                context = {'interview_plan': current.get('interview_plan', {}),
                           'candidate_name': current.get('candidate_name', ''),
                           'evaluation_context': current.get('evaluation_context', {}),
                           'current_question': current.get('current_question', 0),
                           'current_follow_up': current.get('current_follow_up'),
                           'interview_history': current.get('interview_history', [])}
            current['agent_led'] = True
        response = jsonify(context)
        response.headers['Cache-Control'] = 'no-store'
        current_app.logger.info("voice_timing stage=livekit_context_server_ms interview=%s ms=%.2f",
                                interview_id, (time.perf_counter() - started) * 1000)
        return response
    if action == 'event':
        event = service().append_livekit_event(interview_id, generation, body)
        response = jsonify(event_id=event['event_id'], sequence=event['sequence'])
        response.headers['Cache-Control'] = 'no-store'
        return response, 202
    if action == 'finalize':
        with service().journal.edit(interview_id) as current:
            if current.get('livekit_generation') != generation:
                raise ConversationError('This voice session has been replaced.', 409)
        return reply(service().finalize_livekit(interview_id), 202)
    if action == 'state':
        with service().journal.edit(interview_id) as current:
            if current.get('livekit_generation') != generation:
                raise ConversationError('This voice session has been replaced.', 409)
        return reply(service().get(interview_id))
    if action == 'answer':
        return reply(service().accept(interview_id, {
            'turn_id': body.get('turn_id'), 'request_id': body.get('request_id'),
            'answer': body.get('answer')}, livekit_generation=generation), 202)
    return jsonify(error='Unknown action.'), 404


@conversation_bp.get("")
def status():
    started = time.perf_counter()
    interview_id = identity()
    state = service().get(interview_id)
    current_app.logger.info("voice_timing stage=status_server_ms interview=%s ms=%.2f",
                            interview_id, (time.perf_counter() - started) * 1000)
    return reply(state)


@conversation_bp.post("/answer")
@conversation_bp.post("/retry")
def answer():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ConversationError("A JSON object is required.", 400)
    started = time.perf_counter()
    interview_id = identity()
    if payload.get("interview_id") != interview_id:
        raise ConversationError("This answer belongs to a different interview.")
    state = service().accept(interview_id, payload, retry=request.path.endswith("/retry"))
    current_app.logger.warning("voice_timing stage=answer_route_server_ms interview=%s ms=%.2f",
                               interview_id, (time.perf_counter() - started) * 1000)
    return reply(state, 202)


@conversation_bp.post("/control")
def control():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ConversationError("A JSON object is required.", 400)
    interview_id = identity()
    if payload.get('action') == 'end' and service().get(interview_id).get('agent_led'):
        state = service().finalize_livekit(interview_id)
    else:
        state = service().control(interview_id, payload.get("action"))
    if payload.get("action") == "manual":
        copy_to_session(state, session)
        if hasattr(service().journal, "initialize"):
            session["manual_interview_id"] = session.get("voice_interview_id")
            session["manual_interview_token"] = session.get("voice_interview_token")
        session.pop("voice_interview_id", None)
        session.pop("voice_interview_token", None)
    return reply(state)
