"""HTTP conversation protocol. All identities are resolved from the Flask session."""
from flask import Blueprint, current_app, jsonify, request, session
import json
import os
import hmac
from datetime import timedelta
from uuid import uuid4
from itsdangerous import URLSafeTimedSerializer, BadSignature
from app.services.conversation import ConversationError, public_state
from app.services.interview_flow import copy_to_session
from app.auth import enabled as restricted_enabled

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


@conversation_bp.post("/start")
def start():
    return reply(service().start(session, mock=current_app.config.get("VOICE_MOCK_MODE", False)))


@conversation_bp.post('/livekit/session')
def livekit_session():
    """Issue an opt-in room token only to the owner of an active interview."""
    if os.getenv('LIVEKIT_ENABLED', '0') != '1':
        return jsonify(error='LiveKit voice mode is not enabled.'), 404
    interview_id = identity()
    state = service().get(interview_id)
    if state.get('status') != 'ready' or state.get('paused') or state.get('end_requested'):
        raise ConversationError('This interview is not ready for a voice session.', 409)
    url = os.getenv('LIVEKIT_URL', '').strip()
    key = os.getenv('LIVEKIT_API_KEY', '').strip()
    secret = os.getenv('LIVEKIT_API_SECRET', '').strip()
    worker_secret = os.getenv('LIVEKIT_AGENT_SHARED_SECRET', '')
    if not all((url, key, secret)) or len(worker_secret) < 32 or not url.startswith('wss://'):
        return jsonify(error='LiveKit voice mode is not configured.'), 503
    expires_at = None
    if restricted_enabled():
        from app.services.interview_quota import QuotaError, reserve
        try:
            expires_at = reserve(interview_id, session.get('account_name'))
        except QuotaError as error:
            return jsonify(error=str(error)), 429
    from livekit.api import AccessToken, VideoGrants, RoomConfiguration, RoomAgentDispatch
    generation = uuid4().hex
    room = 'interview-' + interview_id + '-' + generation
    with service().journal.edit(interview_id) as current:
        if current.get('status') != 'ready' or current.get('paused') or current.get('end_requested'):
            raise ConversationError('This interview is not ready for a voice session.', 409)
        current['livekit_generation'] = generation
        if expires_at:
            current['livekit_expires_at'] = expires_at.isoformat()
        else:
            current.pop('livekit_expires_at', None)
    agent_name = os.getenv('LIVEKIT_AGENT_NAME', 'ai-interviewer')
    # Job metadata is intentionally limited to the interview identifier; it
    # does not contain a browser session, access token, resume, or credentials.
    ticket = URLSafeTimedSerializer(worker_secret, salt='livekit-interview').dumps({'interview_id': interview_id, 'room': room, 'generation': generation,
                                                                                   'account_version': session.get('account_version')})
    metadata = json.dumps({'ticket': ticket}, separators=(',', ':'))
    token = (AccessToken(key, secret).with_identity('candidate-' + interview_id)
             .with_ttl(timedelta(minutes=5))
             .with_grants(VideoGrants(room_join=True, room=room, can_publish=True,
                                      can_subscribe=True, can_publish_data=True))
             .with_room_config(RoomConfiguration(agents=[RoomAgentDispatch(agent_name=agent_name, metadata=metadata)]))
             .to_jwt())
    response = jsonify(url=url, token=token, room=room,
                       expires_at=expires_at.isoformat() if expires_at else None)
    response.headers['Cache-Control'] = 'no-store'
    return response


@conversation_bp.post('/livekit/agent/<action>')
def livekit_agent(action):
    """Service-to-service access scoped to the server-issued dispatch ticket."""
    secret = os.getenv('LIVEKIT_AGENT_SHARED_SECRET', '')
    supplied = request.headers.get('Authorization', '')
    if os.getenv('LIVEKIT_ENABLED') != '1' or len(secret) < 32 or not hmac.compare_digest(supplied, 'Bearer ' + secret):
        return jsonify(error='Unavailable.'), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get('ticket'), str):
        return jsonify(error='Invalid request.'), 400
    try:
        scope = URLSafeTimedSerializer(secret, salt='livekit-interview').loads(body['ticket'], max_age=7200)
        interview_id = scope['interview_id']
        generation = scope['generation']
        if scope['room'] != 'interview-' + interview_id + '-' + generation:
            raise ValueError('Invalid scope')
    except (BadSignature, KeyError, TypeError, ValueError):
        return jsonify(error='Invalid or expired agent session.'), 403
    if restricted_enabled():
        from app.auth import accounts, account_version
        with service().journal.edit(interview_id) as current:
            configured = accounts()
            owner = current.get('owner_account')
            version = scope.get('account_version')
            if owner not in configured or not isinstance(version, str) or not hmac.compare_digest(version, account_version(configured[owner])):
                return jsonify(error='Interview access is unavailable.'), 403
    if action == 'state':
        with service().journal.edit(interview_id) as current:
            if current.get('livekit_generation') != generation:
                raise ConversationError('This voice session has been replaced.', 409)
        state = service().get(interview_id)
        if restricted_enabled():
            from app.services.interview_quota import active, finish
            if state.get('status') == 'completed':
                finish(interview_id)
            elif not active(interview_id):
                service().control(interview_id, 'end')
                return jsonify(error='Interview time limit reached.'), 410
        return reply(state)
    if action == 'answer':
        if restricted_enabled():
            from app.services.interview_quota import active
            if not active(interview_id):
                return jsonify(error='Interview time limit reached.'), 410
        return reply(service().accept(interview_id, {'turn_id': body.get('turn_id'),
            'request_id': body.get('request_id'), 'answer': body.get('answer')}, livekit_generation=generation), 202)
    return jsonify(error='Unknown action.'), 404


@conversation_bp.get("")
def status():
    return reply(service().get(identity()))


@conversation_bp.post("/answer")
@conversation_bp.post("/retry")
def answer():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ConversationError("A JSON object is required.", 400)
    interview_id = identity()
    if payload.get("interview_id") != interview_id:
        raise ConversationError("This answer belongs to a different interview.")
    return reply(service().accept(interview_id, payload, retry=request.path.endswith("/retry")), 202)


@conversation_bp.post("/control")
def control():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ConversationError("A JSON object is required.", 400)
    state = service().control(identity(), payload.get("action"))
    if payload.get('action') == 'end' and restricted_enabled():
        from app.services.interview_quota import finish
        finish(state['id'])
    if payload.get("action") == "manual":
        copy_to_session(state, session)
        if hasattr(service().journal, "initialize"):
            session["manual_interview_id"] = session.get("voice_interview_id")
            session["manual_interview_token"] = session.get("voice_interview_token")
        session.pop("voice_interview_id", None)
        session.pop("voice_interview_token", None)
    return reply(state)
