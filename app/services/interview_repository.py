"""Transactional PostgreSQL primitives. Callers must explicitly pass ownership.

This repository is not wired to HTTP routes yet. It must not be treated as a
complete replacement for the existing conversation service.
"""
import hashlib
import hmac
import secrets

from sqlalchemy import select

from app.models.interview import ConversationMessage, Interview, InterviewAccess, Question, User


class InterviewNotFound(Exception):
    """Missing or unauthorized interviews deliberately have the same result."""


def token_digest(token):
    if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authorized_interview(session, interview_id, token, *, lock=False):
    digest = token_digest(token)
    statement = (select(Interview, InterviewAccess.token_digest)
                 .join(InterviewAccess, InterviewAccess.interview_id == Interview.id)
                 .where(Interview.id == interview_id))
    if lock:
        statement = statement.with_for_update(of=Interview)
    result = session.execute(statement).first()
    if digest is None or result is None or not hmac.compare_digest(result[1], digest):
        raise InterviewNotFound("Interview not found.")
    return result[0]


def create_interview(session, candidate_name, job_description, resume, plan):
    """Create the plan and one owner capability in the caller's transaction."""
    if not isinstance(plan, dict) or not isinstance(plan.get("questions"), list) or not plan["questions"]:
        raise ValueError("An interview plan with questions is required.")
    token = secrets.token_urlsafe(32)
    owner = User(display_name=candidate_name)
    interview = Interview(owner=owner, job_description=job_description, resume_text=resume, plan=plan)
    session.add(interview)
    session.flush()
    session.add(InterviewAccess(interview_id=interview.id, token_digest=token_digest(token)))
    for sequence, item in enumerate(plan["questions"]):
        session.add(Question(interview_id=interview.id, sequence=sequence,
                             kind=item["type"], skill=item.get("skill") or "General Knowledge",
                             difficulty=item.get("difficulty"), text=item["question"]))
    session.flush()
    return interview.id, token


def append_message(session, interview_id, token, kind, text, question_id=None):
    """Serialize sequence allocation across workers; transaction belongs to caller."""
    authorized_interview(session, interview_id, token, lock=True)
    if question_id is not None:
        question = session.get(Question, question_id)
        if question is None or question.interview_id != interview_id:
            raise InterviewNotFound("Interview not found.")
    previous = session.scalar(select(ConversationMessage.sequence)
                              .where(ConversationMessage.interview_id == interview_id)
                              .order_by(ConversationMessage.sequence.desc()).limit(1))
    message = ConversationMessage(interview_id=interview_id, question_id=question_id,
                                  sequence=0 if previous is None else previous + 1,
                                  kind=kind, text=text)
    session.add(message)
    session.flush()
    return message


def export_transcript(session, interview_id, token):
    authorized_interview(session, interview_id, token)
    messages = session.scalars(select(ConversationMessage)
                               .where(ConversationMessage.interview_id == interview_id)
                               .order_by(ConversationMessage.sequence)).all()
    return [{"sequence": message.sequence, "kind": message.kind, "text": message.text,
             "question_id": message.question_id, "created_at": message.created_at.isoformat()}
            for message in messages]
