"""PostgreSQL voice journal: one short row-locked transaction per transition."""
import time
from contextlib import contextmanager
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.interview import Answer, ConversationMessage, Evaluation, Interview, InterviewRequest, Question, Scorecard, VoiceSnapshot
from app.services.interview_flow import FIELDS, question_for
from app.services.interview_repository import InterviewNotFound, authorized_interview, create_interview


class PostgresVoiceJournal:
    def __init__(self, engine):
        if engine.dialect.name != "postgresql":
            raise ValueError("Voice persistence requires PostgreSQL.")
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def initialize(self, flask_session, mock):
        from uuid import uuid4
        with self.sessions.begin() as db_session:
            context = flask_session.get("evaluation_context", {})
            identity, token = create_interview(db_session, flask_session.get("candidate_name") or "Candidate",
                                               context.get("job_description") or "", context.get("resume"),
                                               flask_session["interview_plan"])
            state = {field: deepcopy(flask_session[field]) for field in FIELDS if field in flask_session}
            if flask_session.get('account_name'):
                state['owner_account'] = flask_session['account_name']
            state.update(id=identity, turn_id=uuid4().hex, status="ready", paused=False, jobs={}, mock=bool(mock),
                         draft="", error=None, timings={}, created_at=time.time(), updated_at=time.time(), version=1)
            if question_for(state) is None:
                state["status"] = "completed" if state.get("final_scorecard") else "error"
            db_session.add(VoiceSnapshot(interview_id=identity, state=deepcopy(state)))
            self._project(db_session, identity, state)
        return identity, token

    def authorize(self, identity, token, account_name=None):
        from app.services.conversation import ConversationError
        try:
            with self.sessions.begin() as session:
                authorized_interview(session, identity, token)
                if account_name is not None:
                    snapshot = session.get(VoiceSnapshot, identity)
                    if snapshot is None or snapshot.state.get('owner_account') != account_name:
                        raise InterviewNotFound('Interview not found.')
        except InterviewNotFound as error:
            raise ConversationError("Interview not found.", 404) from error

    def purge(self, retention_seconds):
        # Interview retention is deliberately managed separately, never by a read.
        return None

    @contextmanager
    def edit(self, identity, create=False):
        from app.services.conversation import ConversationError, IDENTIFIER
        if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity):
            raise ConversationError("Invalid interview identifier.", 400)
        with self.sessions.begin() as session:
            # Lock the parent first for serialization, including across workers.
            interview = session.scalar(select(Interview).where(Interview.id == identity).with_for_update())
            snapshot = session.get(VoiceSnapshot, identity)
            if interview is None or snapshot is None:
                raise ConversationError("Interview expired. Start again from the setup page.", 410)
            state = deepcopy(snapshot.state)
            before = deepcopy(state)
            yield state
            if state != before:
                state["updated_at"] = time.time()
                state["version"] = state.get("version", 0) + 1
                snapshot.state = deepcopy(state)
                self._project(session, identity, state)

    def _project(self, session, identity, state):
        interview = session.get(Interview, identity)
        interview.status = state["status"]
        interview.current_question = state.get("current_question", 0)
        session.flush()
        mains = session.scalars(select(Question).where(Question.interview_id == identity, Question.parent_id.is_(None)).order_by(Question.sequence)).all()
        events = []
        history = state.get("interview_history", [])
        pending_follow = state.get("current_follow_up")
        for index, main in enumerate(mains):
            if index >= len(history) and index != state.get("current_question", 0):
                break
            events.append(("question", main.text, main.id))
            if index >= len(history):
                break
            item = history[index]
            answer = session.scalar(select(Answer).where(Answer.question_id == main.id))
            if answer is None:
                answer = Answer(question_id=main.id, status="unanswered" if item.get("status") == "Unanswered" else "answered", transcript=item.get("answer", ""))
                session.add(answer)
                session.flush()
            events.append((answer.status, answer.transcript or "", main.id))
            evaluation = item.get("evaluation")
            if evaluation and session.scalar(select(Evaluation.id).where(Evaluation.answer_id == answer.id)) is None:
                session.add(Evaluation(answer_id=answer.id, score=evaluation["score"], strengths=evaluation.get("strengths", []),
                                       weaknesses=evaluation.get("weaknesses", []), feedback=evaluation.get("feedback", ""),
                                       follow_up_needed=evaluation.get("follow_up_needed", False), follow_up_question=evaluation.get("follow_up_question")))
            follow = item.get("follow_up")
            follow_text = follow.get("question") if follow else (pending_follow.get("question") if pending_follow and index == state.get("current_question") else None)
            if follow_text:
                child = session.scalar(select(Question).where(Question.parent_id == main.id))
                if child is None:
                    child = Question(interview_id=identity, parent_id=main.id, sequence=index, kind="follow_up", skill=main.skill, text=follow_text)
                    session.add(child)
                    session.flush()
                events.append(("follow_up", child.text, child.id))
                if follow is not None:
                    follow_answer = session.scalar(select(Answer).where(Answer.question_id == child.id))
                    if follow_answer is None:
                        follow_answer = Answer(question_id=child.id, status="unanswered" if follow.get("status") == "Unanswered" else "answered", transcript=follow.get("answer", ""))
                        session.add(follow_answer)
                        session.flush()
                    events.append((follow_answer.status, follow_answer.transcript or "", child.id))
        if state.get("status") == "completed":
            events.append(("completion", "Interview ended early" if state.get("early_ended") else "Interview completed", None))
        existing = session.scalars(select(ConversationMessage).where(ConversationMessage.interview_id == identity).order_by(ConversationMessage.sequence)).all()
        for sequence in range(len(existing), len(events)):
            kind, text, question_id = events[sequence]
            session.add(ConversationMessage(interview_id=identity, sequence=sequence, kind=kind, text=text, question_id=question_id))
        for key, job in state.get("jobs", {}).items():
            record = session.get(InterviewRequest, key)
            if record is None:
                record = InterviewRequest(id=key, interview_id=identity, turn_id=job["turn_id"], digest=job["digest"], stage=job["stage"], status=job["status"])
                session.add(record)
            else:
                if record.interview_id != identity:
                    raise ValueError("Request identifier belongs to another interview.")
                record.status, record.stage = job["status"], job["stage"]
        if state.get("final_scorecard") and session.scalar(select(Scorecard.id).where(Scorecard.interview_id == identity)) is None:
            session.add(Scorecard(interview_id=identity, result=state["final_scorecard"], completion_status=state["status"], early_ended=bool(state.get("early_ended"))))
