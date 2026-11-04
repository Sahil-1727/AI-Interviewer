"""HTTP conversation protocol. All identities are resolved from the Flask session."""
from flask import Blueprint, current_app, jsonify, request, session
import json
import os
from datetime import timedelta
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
    if not all((url, key, secret)) or not url.startswith('wss://'):
        return jsonify(error='LiveKit voice mode is not configured.'), 503
    from livekit.api import AccessToken, VideoGrants, RoomConfiguration, RoomAgentDispatch
    room = 'interview-' + interview_id
    agent_name = os.getenv('LIVEKIT_AGENT_NAME', 'ai-interviewer')
    # Job metadata is intentionally limited to the interview identifier; it
    # does not contain a browser session, access token, resume, or credentials.
    metadata = json.dumps({'interview_id': interview_id}, separators=(',', ':'))
    token = (AccessToken(key, secret).with_identity('candidate-' + interview_id)
             .with_ttl(timedelta(minutes=5))
             .with_grants(VideoGrants(room_join=True, room=room, can_publish=True,
                                      can_subscribe=True, can_publish_data=False))
             .with_room_config(RoomConfiguration(agents=[RoomAgentDispatch(agent_name=agent_name, metadata=metadata)]))
             .to_jwt())
    response = jsonify(url=url, token=token, room=room)
    response.headers['Cache-Control'] = 'no-store'
    return response


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
    if payload.get("action") == "manual":
        copy_to_session(state, session)
        if hasattr(service().journal, "initialize"):
            session["manual_interview_id"] = session.get("voice_interview_id")
            session["manual_interview_token"] = session.get("voice_interview_token")
        session.pop("voice_interview_id", None)
        session.pop("voice_interview_token", None)
    return reply(state)
