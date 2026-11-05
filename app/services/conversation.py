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
from datetime import datetime, timezone
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
            self.journal.authorize(identity, session.get("manual_interview_token"), session.get('account_name'))
            snapshot = None
            with self.journal.edit(identity) as state:
                if state.get("status") == "manual":
                    state.update(status="ready", paused=False)
                    snapshot = deepcopy(state)
            session["voice_interview_id"] = identity
            session["voice_interview_token"] = session.pop("manual_interview_token")
            session.pop("manual_interview_id", None)
            # Reuse the locked row snapshot after the manual-to-voice handoff;
            # a second read transaction is unnecessary on this hot path.
            return snapshot if snapshot is not None else self.get(identity)
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
            self.journal.authorize(identity, session.get("voice_interview_token"), session.get('account_name'))

    def append_livekit_event(self, identity, generation, payload):
        """Store one immutable agent event under the interview's existing lock.

        Events stay separate from the existing answer history during live
        conversation, then finalize_livekit reconciles completed Q/A pairs for
        the existing evaluator and scorecard service.
        """
        event_id, turn_id = payload.get('event_id'), payload.get('turn_id')
        kind, text = payload.get('kind'), payload.get('text')
        if (not isinstance(event_id, str) or not IDENTIFIER.fullmatch(event_id)
                or not isinstance(turn_id, str) or not IDENTIFIER.fullmatch(turn_id)
                or kind not in {'candidate', 'interviewer'} or not isinstance(text, str)
                or not text.strip() or len(text) > self.settings['MAX_ANSWER_CHARACTERS']):
            raise ConversationError('Invalid interview event.', 400)
        event = {'event_id': event_id, 'turn_id': turn_id, 'kind': kind, 'text': text}
        with self.journal.edit(identity) as state:
            if state.get('livekit_generation') != generation:
                raise ConversationError('This voice session has been replaced.', 409)
            if state.get('livekit_expires_at'):
                try:
                    deadline = datetime.fromisoformat(state['livekit_expires_at'])
                    expired = deadline.tzinfo is None or datetime.now(timezone.utc) >= deadline
                except (TypeError, ValueError):
                    expired = True
                if expired:
                    raise ConversationError('Interview time limit reached.', 410)
            events = state.setdefault('livekit_events', [])
            existing = next((item for item in events if item['event_id'] == event_id), None)
            if existing:
                if any(existing[key] != value for key, value in event.items()):
                    raise ConversationError('Event identifier already belongs to another event.', 409)
                return deepcopy(existing)
            if state.get('end_requested') or state.get('status') in {'ended', 'completed'}:
                raise ConversationError('This interview has ended.', 410)
            if len(events) >= 1000:
                raise ConversationError('Interview event limit reached.', 409)
            event.update(sequence=len(events), created_at=time.time())
            events.append(event)
            return deepcopy(event)

    @staticmethod
    def reconcile_livekit_transcript(snapshot):
        """Pair finalized agent/candidate messages without counting voice commands.

        The agent is instructed to use approved main-question wording verbatim;
        unmatched questions after a completed main turn are treated as follow-ups
        of that main question.
        """
        plan = snapshot.get('interview_plan', {}).get('questions', [])
        if not plan:
            return [], set(), 0, None
        answered_main, rows = set(), []
        active_question = None
        active_question_id = None
        current_main = None
        for event in snapshot.get('livekit_events', []):
            if event.get('kind') == 'interviewer':
                active_question = event.get('text', '')
                active_question_id = event.get('event_id')
                continue
            if event.get('kind') != 'candidate' or not active_question:
                continue
            answer = (event.get('text') or '').strip()
            normalized_answer = answer.casefold().replace("i'm", 'i am').replace('im done', 'i am done')
            command = re.sub(r'[^a-z0-9 ]', '', normalized_answer).strip()
            if command in {'repeat', 'repeat question', 'repeat the question', 'please repeat the question',
                           'can you repeat', 'can you repeat the question', 'could you repeat',
                           'could you repeat the question', 'i am done',
                           'i am done speaking', 'done speaking'}:
                continue
            normalized = re.sub(r'[^a-z0-9 ]', '', active_question.casefold()).strip()
            matched = next((index for index, item in enumerate(plan)
                            if re.sub(r'[^a-z0-9 ]', '', str(item.get('question', '')).casefold()).strip()
                            and re.sub(r'[^a-z0-9 ]', '', str(item.get('question', '')).casefold()).strip() in normalized), None)
            if matched is not None and matched not in answered_main:
                plan_index, kind = matched, str(plan[matched].get('type') or 'technical')
                current_main = plan_index
                answered_main.add(plan_index)
                parent_index = None
            elif current_main is not None and current_main in answered_main:
                plan_index, kind, parent_index = current_main, 'follow_up', current_main
            else:
                plan_index = next((index for index in range(len(plan)) if index not in answered_main),
                                  max(0, min(snapshot.get('current_question', 0), len(plan) - 1)))
                kind, parent_index = str(plan[plan_index].get('type') or 'technical'), None
                current_main = plan_index
                answered_main.add(plan_index)
            skill = (plan[plan_index].get('skill') or 'General Knowledge') if plan else 'General Knowledge'
            rows.append({'question': active_question, 'answer': answer, 'skill': skill, 'type': kind,
                         'plan_index': plan_index, 'parent_plan_index': parent_index,
                         'question_event_id': active_question_id, 'answer_event_id': event.get('event_id'),
                         'evaluation_context': snapshot.get('evaluation_context', {})})
            active_question = None
            active_question_id = None
        pending_question = active_question
        next_main = next((index for index in range(len(plan)) if index not in answered_main),
                         max(0, len(plan) - 1))
        return rows, answered_main, next_main, pending_question

    def finalize_livekit(self, identity):
        """Evaluate committed LiveKit turns and build a scorecard in background."""
        request_id = None
        with self.journal.edit(identity) as state:
            if not state.get('agent_led'):
                raise ConversationError('Agent-led interview mode is not active.')
            if state.get('status') == 'completed':
                return deepcopy(state)
            if state.get('livekit_finalize_request') and state.get('status') == 'scoring':
                if time.time() - state.get('updated_at', time.time()) <= self.settings['CONVERSATION_STALE_SECONDS']:
                    return deepcopy(state)
                state.update(status='error', paused=True,
                             error='Scorecard processing was interrupted. Retry ending the interview.')
            state.pop('livekit_finalize_request', None)
            if not self.slots.acquire(blocking=False):
                raise ConversationError('Interview service is busy. Retry shortly.', 503)
            request_id = uuid4().hex
            state.update(end_requested=True, early_ended=True, paused=True,
                         status='scoring', livekit_finalize_request=request_id, error=None)
            state.pop('finalization_error_stage', None)
        try:
            self.executor.submit(self._run_livekit_finalize, identity, request_id)
        except Exception:
            self.slots.release()
            with self.journal.edit(identity) as state:
                state.update(status='error', error='Scorecard processing could not start. Retry ending the interview.')
        return self.get(identity)

    def _run_livekit_finalize(self, identity, request_id):
        finalize_started = time.perf_counter()
        stage = 'load_transcript'
        try:
            with self.journal.edit(identity) as state:
                if state.get('livekit_finalize_request') != request_id:
                    return
                snapshot = deepcopy(state)
            pairs, answered_main, _next_main, _pending = self.reconcile_livekit_transcript(snapshot)
            plan_questions = snapshot.get('interview_plan', {}).get('questions', [])
            evaluations = snapshot.get('livekit_evaluations', {})
            cached_keys = {
                hashlib.sha256(f"{row['question_event_id']}:{row['answer_event_id']}".encode('utf-8')).hexdigest()
                for row in pairs
            }
            cached_count = sum(key in evaluations for key in cached_keys)

            def evaluate_pair(index, row):
                question = {'question': row['question'], 'skill': row['skill'], 'type': row['type'],
                            'evaluation_context': row['evaluation_context']}
                cache_key = hashlib.sha256(
                    f"{row['question_event_id']}:{row['answer_event_id']}".encode('utf-8')).hexdigest()
                evaluation = evaluations.get(cache_key)
                if evaluation is None:
                    evaluation = (mock_evaluate(question, row['answer']) if snapshot.get('mock')
                                  else self.evaluator(question, row['answer']))
                    with self.journal.edit(identity) as state:
                        if state.get('livekit_finalize_request') != request_id:
                            return None
                        state.setdefault('livekit_evaluations', {})[cache_key] = deepcopy(evaluation)
                    evaluations[cache_key] = deepcopy(evaluation)
                return evaluation

            evaluation_started = time.perf_counter()
            concurrency = min(int(self.settings['LIVEKIT_FINALIZE_EVALUATION_CONCURRENCY']), max(1, len(pairs)))
            if pairs:
                # Finalization used to call Gemini once per answer in series.
                # Evaluate independent transcript pairs concurrently, but
                # persist each result immediately so a retry only repeats the
                # specific evaluations that failed.
                stage = 'evaluate_answer'
                with ThreadPoolExecutor(max_workers=concurrency,
                                        thread_name_prefix='livekit-evaluation') as pool:
                    futures = [pool.submit(evaluate_pair, index, row) for index, row in enumerate(pairs)]
                    pair_evaluations = [future.result() for future in futures]
            else:
                pair_evaluations = []
            if any(result is None for result in pair_evaluations):
                return
            evaluation_ms = round((time.perf_counter() - evaluation_started) * 1000, 2)
            logger.info('voice_timing stage=livekit_evaluations interview=%s count=%d cached=%d concurrency=%d ms=%.2f',
                        identity, len(pairs), cached_count, concurrency, evaluation_ms)
            history = [
                {key: row[key] for key in
                 ('question', 'answer', 'skill', 'type', 'plan_index', 'parent_plan_index')} |
                {'evaluation': evaluation}
                for row, evaluation in zip(pairs, pair_evaluations)
            ]
            evaluated = [item for item in history if item.get('evaluation')]
            evaluated_main = {item['plan_index'] for item in history
                              if item.get('evaluation') and item.get('type') != 'follow_up'}
            stage = 'generate_scorecard'
            started_at = time.perf_counter()
            scorecard = ({'insufficient_evidence': True} if not evaluated else
                         mock_scorecard(snapshot.get('candidate_name', 'Candidate'), evaluated)
                         if snapshot.get('mock') else self.scorecard(snapshot.get('candidate_name'), evaluated))
            scorecard_ms = round((time.perf_counter() - started_at) * 1000, 2)
            if not isinstance(scorecard, dict) or (
                    not scorecard.get('insufficient_evidence') and
                    not isinstance(scorecard.get('overall_score'), (int, float))):
                raise ValueError('Invalid scorecard response')
            stage = 'save_scorecard'
            with self.journal.edit(identity) as state:
                if state.get('livekit_finalize_request') != request_id:
                    return
                state['interview_history'] = history
                state['current_question'] = min(len(plan_questions), len(evaluated_main))
                state.setdefault('timings', {})['scorecard_ms'] = scorecard_ms
                state.update(status='completed', final_scorecard=scorecard, draft='', error=None,
                             early_ended=len(evaluated_main) < len(plan_questions))
            logger.info('voice_timing stage=livekit_scorecard interview=%s ms=%.2f mock=%s',
                        identity, scorecard_ms, snapshot.get('mock'))
            logger.info('voice_timing stage=livekit_finalization interview=%s ms=%.2f evaluated=%d',
                        identity, round((time.perf_counter() - finalize_started) * 1000, 2), len(evaluated))
        except Exception as error:
            logger.error('LiveKit finalization failed: interview=%s stage=%s error_type=%s',
                         identity, stage, type(error).__name__)
            with self.journal.edit(identity) as state:
                if state.get('livekit_finalize_request') == request_id:
                    state.update(status='error', paused=True,
                                 error='Final interview processing failed. Retry finalization.',
                                 finalization_error_stage=stage)
        finally:
            self.slots.release()

    def _recover_stale(self, state):
        if (state.get('status') == 'scoring' and state.get('livekit_finalize_request')
                and time.time() - state.get('updated_at', time.time()) > self.settings['CONVERSATION_STALE_SECONDS']):
            state.update(status='error', paused=True,
                         error='Scorecard processing was interrupted. Retry ending the interview.')
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

    def accept(self, identity, payload, retry=False, livekit_generation=None):
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
            if livekit_generation is not None and state.get('livekit_generation') != livekit_generation:
                raise ConversationError('This voice session has been replaced.', 409)
            if livekit_generation is not None and state.get('livekit_expires_at'):
                try:
                    deadline = datetime.fromisoformat(state['livekit_expires_at'])
                    expired = deadline.tzinfo is None or datetime.now(timezone.utc) >= deadline
                except (TypeError, ValueError):
                    expired = True
                if expired:
                    raise ConversationError('Interview time limit reached.', 410)
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
        processing_stage = 'claim'
        try:
            with self.journal.edit(identity) as state:
                job = state["jobs"][request_id]
                if job["status"] != "queued":
                    return
                job.update(status="running", updated_at=time.time())
                work = deepcopy(state)
            job = work["jobs"][request_id]
            if job["stage"] == "evaluation":
                processing_stage = 'evaluation'
                start = time.perf_counter()
                evaluator = mock_evaluate if work["mock"] else self.evaluator
                next_question = advance(work, job["answer"], evaluator, unanswered=job.get("unanswered", False))
                elapsed = (time.perf_counter() - start) * 1000
                processing_stage = 'save_evaluation'
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
            processing_stage = 'scorecard'
            start = time.perf_counter()
            evaluated = [item for item in work["interview_history"] if item.get("evaluation")]
            scorecard = ({"insufficient_evidence": True} if not evaluated else
                         mock_scorecard(work["candidate_name"], evaluated) if work["mock"] else self.scorecard(work["candidate_name"], evaluated))
            if not isinstance(scorecard, dict) or (not scorecard.get("insufficient_evidence") and not isinstance(scorecard.get("overall_score"), (int, float))):
                raise ValueError("Invalid scorecard response")
            elapsed = (time.perf_counter() - start) * 1000
            processing_stage = 'save_scorecard'
            with self.journal.edit(identity) as state:
                if state.get("active_request") != request_id or state["status"] != "scoring":
                    return
                state["timings"]["scorecard_ms"] = round(elapsed, 2)
                state.update(status="completed", final_scorecard=scorecard, error=None, draft="")
                state["jobs"][request_id].update(status="done", updated_at=time.time())
                state.pop("retry_stage", None)
            logger.info("voice_timing stage=scorecard interview=%s ms=%.2f mock=%s", identity, elapsed, work["mock"])
        except Exception as error:
            # Provider errors can include credentials or candidate content. Log
            # only the failure stage and exception class, never the message.
            logger.error("Conversation processing failed: interview=%s request=%s stage=%s error_type=%s",
                         identity, request_id, processing_stage, type(error).__name__)
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
            "finalization_error_stage": state.get("finalization_error_stage"),
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
