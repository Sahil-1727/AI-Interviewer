"""HTTP conversation protocol. All identities are resolved from the Flask session."""
from flask import Blueprint, current_app, jsonify, request, session
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
        session.pop("voice_interview_id", None)
        session.pop("voice_interview_token", None)
    return reply(state)
