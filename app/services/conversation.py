"""Single-host conversation journal and bounded background orchestration.

An OS file lock serializes claims across threads AND workers sharing the same
local directory. Atomic JSON snapshots survive process restarts. External Gemini
calls cannot participate in a filesystem transaction: an interrupted/ambiguous
call is NEVER automatically retried. Explicit recovery creates a new attempt.
This is a temporary single-host mechanism, not a distributed job queue/database.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from threading import BoundedSemaphore
import time
from uuid import uuid4

from app.services.interview_flow import FIELDS, advance, question_for
from app.services.voice_commands import COMMANDS, normalize

logger = logging.getLogger(__name__)
IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")


class ConversationError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def edit(self, identity, create=False):
        if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity):
            raise ConversationError("Invalid interview identifier.", 400)
        path = self.directory / f"{identity}.json"
        descriptor = os.open(self.directory / f"{identity}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not path.exists() and not create:
                raise ConversationError("Interview expired. Start again from the setup page.", 410)
            state = json.loads(path.read_text()) if path.exists() else {}
            before = deepcopy(state)
            yield state
            if state != before:
                state["updated_at"] = time.time()
                state["version"] = state.get("version", 0) + 1
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, suffix=".tmp", delete=False) as file:
                        temporary = file.name
                        json.dump(state, file, allow_nan=False)
                        file.flush()
                        os.fsync(file.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)

    def purge(self, retention_seconds):
        """Opportunistic bounded cleanup. Never delete a locked/live journal."""
        cutoff = time.time() - retention_seconds
        for path in list(self.directory.glob("*.json"))[:100]:
            try:
                modified = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if modified >= cutoff:
                continue
            descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if path.exists() and path.stat().st_mtime < cutoff:
                    path.unlink()
                # Lock inodes stay stable: unlinking them would break exclusion.


class ConversationService:
    def __init__(self, directory, evaluator, scorecard, settings, journal=None):
        self.journal = journal or Journal(directory)
        self.evaluator = evaluator
        self.scorecard = scorecard
        self.settings = settings
        self.executor = ThreadPoolExecutor(max_workers=int(settings["CONVERSATION_WORKERS"]), thread_name_prefix="interview")
        self.slots = BoundedSemaphore(int(settings["CONVERSATION_QUEUE_LIMIT"]))

    def start(self, session, mock=False):
        if not session.get("interview_plan", {}).get("questions"):
            raise ConversationError("Set up an interview first.", 400)
        identity = session.get("voice_interview_id")
        if identity:
            self.authorize(identity, session)
            return self.get(identity)
        if hasattr(self.journal, "initialize") and session.get("manual_interview_id"):
            identity = session["manual_interview_id"]
            self.journal.authorize(identity, session.get("manual_interview_token"))
            with self.journal.edit(identity) as state:
                if state.get("status") == "manual":
                    state.update(status="ready", paused=False)
            session["voice_interview_id"] = identity
            session["voice_interview_token"] = session.pop("manual_interview_token")
            session.pop("manual_interview_id", None)
            return self.get(identity)
        if hasattr(self.journal, "initialize"):
            identity, token = self.journal.initialize(session, mock)
            session["voice_interview_id"] = identity
            session["voice_interview_token"] = token
            return self.get(identity)
        self.journal.purge(self.settings["CONVERSATION_RETENTION_SECONDS"])
        identity = uuid4().hex
        with self.journal.edit(identity, create=True) as state:
            state.update({field: deepcopy(session[field]) for field in FIELDS if field in session})
            state.update(id=identity, turn_id=uuid4().hex, status="ready", paused=False, jobs={}, mock=bool(mock),
                         draft="", error=None, timings={}, created_at=time.time())
            if question_for(state) is None:
                state["status"] = "completed" if state.get("final_scorecard") else "error"
        session["voice_interview_id"] = identity
        return self.get(identity)

    def authorize(self, identity, session):
        if hasattr(self.journal, "authorize"):
            self.journal.authorize(identity, session.get("voice_interview_token"))

    def _recover_stale(self, state):
        active = state.get("active_request")
        if state.get("status") in {"evaluating", "scoring"} and active:
            job = state["jobs"][active]
            if time.time() - job["updated_at"] > self.settings["CONVERSATION_STALE_SECONDS"]:
                job["status"] = "error"
                state.update(status="error", paused=True, error="Processing was interrupted. Your answer is saved. Retry processing explicitly or use manual mode.")

    def get(self, identity):
        with self.journal.edit(identity) as state:
            self._recover_stale(state)
            result = deepcopy(state)
        if result.get('status') == 'ending':
            self._schedule_early_scorecard(identity)
            with self.journal.edit(identity) as state:
                return deepcopy(state)
        return result

    def _schedule_early_scorecard(self, identity):
        """Claim a single scorecard job after termination; polling retries admission."""
        request_id = None
        with self.journal.edit(identity) as state:
            if state.get("status") != "ending" or not self.slots.acquire(blocking=False):
                return
            request_id = uuid4().hex
            state["jobs"][request_id] = {
                "digest": "early-scorecard", "turn_id": state["turn_id"],
                "answer": state["draft"], "stage": "scorecard", "status": "queued",
                "updated_at": time.time(),
            }
            state.update(status="scoring", active_request=request_id, retry_stage="scorecard")
        try:
            self.executor.submit(self._run, identity, request_id)
        except Exception:
            self.slots.release()
            self._error(identity, request_id, "Scorecard processing could not start. Retry processing.")

    def accept(self, identity, payload, retry=False):
        turn_id, request_id, answer = payload.get("turn_id"), payload.get("request_id"), payload.get("answer")
        unanswered = payload.get("unanswered") is True and not retry
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ConversationError("A unique request_id is required.", 400)
        if not isinstance(answer, str) or (not answer.strip() and not unanswered and not retry) or (unanswered and answer.strip()) or len(answer) > self.settings["MAX_ANSWER_CHARACTERS"]:
            raise ConversationError("Provide a nonempty answer within the configured length limit.", 400)
        answer = answer.strip()
        digest = hashlib.sha256(json.dumps([turn_id, answer, unanswered]).encode()).hexdigest()
        with self.journal.edit(identity) as state:
            self._recover_stale(state)
            if state.get("end_requested") and not (retry and state.get("retry_stage") == "scorecard"):
                raise ConversationError("This interview has ended.")
            prior = state["jobs"].get(request_id)
            if prior:
                if prior["digest"] != digest:
                    raise ConversationError("This request identifier belongs to a different answer.")
                return deepcopy(state)
            if turn_id != state["turn_id"]:
                raise ConversationError("That question has already advanced. Reload the interview state.")
            if state["status"] != ("error" if retry else "ready"):
                raise ConversationError("This turn is already processing or is unavailable.")
            if state.get("paused") and not retry:
                raise ConversationError("Resume the interview before submitting.")
            if not retry:
                for command, phrases in COMMANDS.items():
                    if normalize(answer) in phrases:
                        # Defense in depth for low-confidence STT commands:
                        # control-only utterances must never consume Gemini.
                        return dict(deepcopy(state), control_action=command)
            if question_for(state) is None and not (retry and state.get("retry_stage") == "scorecard"):
                raise ConversationError("The interview is complete.")
            if not self.slots.acquire(blocking=False):
                raise ConversationError("The interview service is busy. Your answer is kept; retry shortly.", 503)
            stage = state.get("retry_stage", "evaluation") if retry else "evaluation"
            job = {"digest": digest, "turn_id": turn_id, "answer": answer, "stage": stage,
                   "unanswered": unanswered, "status": "queued", "updated_at": time.time()}
            state["jobs"][request_id] = job
            state.update(status="scoring" if stage == "scorecard" else "evaluating", active_request=request_id,
                         draft=answer, error=None, retry_stage=stage)
        try:
            self.executor.submit(self._run, identity, request_id)
        except Exception:
            self.slots.release()
            self._error(identity, request_id, "Processing could not start. Retry processing.")
        return self.get(identity)

    def _error(self, identity, request_id, message):
        with self.journal.edit(identity) as state:
            job = state["jobs"][request_id]
            if state.get("active_request") == request_id:
                job.update(status="error", updated_at=time.time())
                state.update(status="error", paused=True, error=message, retry_stage=job["stage"])

    def _run(self, identity, request_id):
        try:
            with self.journal.edit(identity) as state:
                job = state["jobs"][request_id]
                if job["status"] != "queued":
                    return
                job.update(status="running", updated_at=time.time())
                work = deepcopy(state)
            job = work["jobs"][request_id]
            if job["stage"] == "evaluation":
                start = time.perf_counter()
                evaluator = mock_evaluate if work["mock"] else self.evaluator
                next_question = advance(work, job["answer"], evaluator, unanswered=job.get("unanswered", False))
                elapsed = (time.perf_counter() - start) * 1000
                with self.journal.edit(identity) as state:
                    if state.get("active_request") != request_id or state["status"] != "evaluating":
                        return
                    for field in FIELDS:
                        if field in work:
                            state[field] = work[field]
                        else:
                            state.pop(field, None)
                    state["timings"] = {"evaluation_ms": round(elapsed, 2)}
                    logger.info("voice_timing stage=evaluation interview=%s ms=%.2f mock=%s", identity, elapsed, work["mock"])
                    if next_question:
                        state.update(status="ended" if state.get("end_requested") else "ready", turn_id=uuid4().hex, draft="", error=None)
                        state["jobs"][request_id].update(status="done", updated_at=time.time())
                        state.pop("retry_stage", None)
                        return
                    state.update(status="scoring", retry_stage="scorecard")
                    state["jobs"][request_id].update(stage="scorecard", updated_at=time.time())
                    work = deepcopy(state)
            start = time.perf_counter()
            evaluated = [item for item in work["interview_history"] if item.get("evaluation")]
            scorecard = ({"insufficient_evidence": True} if not evaluated else
                         mock_scorecard(work["candidate_name"], evaluated) if work["mock"] else self.scorecard(work["candidate_name"], evaluated))
            if not isinstance(scorecard, dict) or (not scorecard.get("insufficient_evidence") and not isinstance(scorecard.get("overall_score"), (int, float))):
                raise ValueError("Invalid scorecard response")
            elapsed = (time.perf_counter() - start) * 1000
            with self.journal.edit(identity) as state:
                if state.get("active_request") != request_id or state["status"] != "scoring":
                    return
                state["timings"]["scorecard_ms"] = round(elapsed, 2)
                state.update(status="completed", final_scorecard=scorecard, error=None, draft="")
                state["jobs"][request_id].update(status="done", updated_at=time.time())
                state.pop("retry_stage", None)
            logger.info("voice_timing stage=scorecard interview=%s ms=%.2f mock=%s", identity, elapsed, work["mock"])
        except Exception:
            logger.error("Conversation processing failed: interview=%s request=%s", identity, request_id)
            self._error(identity, request_id, "Processing failed or timed out. Your answer is saved. Retry processing or switch to manual mode.")
        finally:
            self.slots.release()

    def control(self, identity, action):
        with self.journal.edit(identity) as state:
            self._recover_stale(state)
            if action in {"pause", "resume"}:
                if state["status"] in {"ended", "manual", "completed"} or state.get("end_requested"):
                    raise ConversationError("This conversation has ended.")
                state["paused"] = action == "pause"
            elif action == "end":
                if not state.get("end_requested") and state["status"] != "completed":
                    state.update(end_requested=True, early_ended=True, paused=True, error=None)
                    # A scorecard already in flight covers committed history.
                    if state["status"] != "scoring":
                        active = state.get("active_request")
                        if active and state["jobs"][active]["status"] in {"queued", "running"}:
                            state["jobs"][active]["status"] = "cancelled"
                        state.pop("active_request", None)
                        history = [item for item in state.get("interview_history", []) if item.get("evaluation")]
                        if history:
                            state.update(status="ending", retry_stage="scorecard", draft=history[-1]["answer"])
                        else:
                            state.update(status="completed", final_scorecard={"insufficient_evidence": True}, draft="")
            elif action == "manual":
                if state["status"] in {"evaluating", "scoring"}:
                    # End stops future progression but doesn't pretend to cancel
                    # an external request already in flight. Manual waits for it.
                    raise ConversationError("Wait for the current response before switching to manual mode.")
                else:
                    if state.get("end_requested"):
                        raise ConversationError("This interview has ended.")
                    state.update(status="manual", paused=True)
            else:
                raise ConversationError("Unknown control.", 400)
        return self.get(identity)


def public_state(state):
    question = question_for(state)
    return {"interview_id": state["id"], "turn_id": state["turn_id"], "status": state["status"],
            "paused": state["paused"], "question": question, "is_follow_up": bool(state.get("current_follow_up")),
            "question_number": state.get("current_question", 0) + 1,
            "completed_questions": state.get("current_question", 0),
            "total_questions": len(state["interview_plan"]["questions"]),
            "draft": state.get("draft", ""), "error": state.get("error"), "timings": state.get("timings", {}),
            "mock": state["mock"], "version": state.get("version", 0),
            "end_requested": state.get("end_requested", False),
            "early_ended": state.get("early_ended", False),
            "control_action": state.get("control_action"),
            "scorecard_url": "/final-scorecard" if state["status"] == "completed" else None}


def mock_evaluate(question, answer):
    follow_up = question.get("mock_follow_up", False)
    return {"score": 7, "strengths": ["Clear example"], "weaknesses": ["Add measurable results"],
            "feedback": "Deterministic mock evaluation.", "follow_up_needed": follow_up,
            "follow_up_question": "What did you learn from that project?" if follow_up else ""}


def mock_scorecard(candidate, history):
    return {"overall_score": 7, "competency_scores": [], "strengths": ["Completed the conversation"],
            "areas_to_improve": ["Add measurable results"], "communication_assessment": "Mock communication assessment.",
            "technical_assessment": "Mock technical assessment.", "recommendation": "Consider",
            "final_summary": f"Mock interview completed with {len(history)} main questions. No Gemini calls were made."}
