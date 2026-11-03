"""Actual voice endpoints against a dedicated migrated PostgreSQL database."""
import os
import io
import re
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select

import run
from app.models.interview import Answer, ConversationMessage, Evaluation, Interview, Question, Scorecard, VoiceSnapshot
from app.services.conversation import ConversationService, mock_evaluate, mock_scorecard
from app.services.postgres_voice import PostgresVoiceJournal


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "requires dedicated PostgreSQL database")
class PostgreSQLVoiceEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = run.app.test_client()
        with self.client.session_transaction() as session:
            session.clear()
            session.update(candidate_name="Candidate", evaluation_context={"job_description": "Java basics", "resume": ""},
                interview_plan={"questions": [
                    {"question": "Explain Java", "skill": "Java", "type": "technical", "mock_follow_up": True},
                    {"question": "Explain a class", "skill": "Java", "type": "technical"}]},
                current_question=0, interview_history=[])

    def wait(self):
        for _ in range(300):
            response = self.client.get('/api/conversation')
            if response.json['status'] not in {'evaluating', 'scoring', 'ending'}:
                return response.json
            time.sleep(.01)
        self.fail('Voice job did not complete')

    def submit(self, state, answer):
        payload = dict(interview_id=state['interview_id'], turn_id=state['turn_id'], request_id=uuid4().hex, answer=answer)
        response = self.client.post('/api/conversation/answer', json=payload)
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return self.wait(), payload

    def test_full_voice_interview_and_isolation(self):
        def evaluate_fractional(question, answer):
            return {**mock_evaluate(question, answer), 'score': 7.5}
        with patch.object(run, 'evaluate_answer', side_effect=evaluate_fractional) as evaluate, patch.object(run, 'generate_final_scorecard', side_effect=mock_scorecard) as score:
            state = self.client.post('/api/conversation/start', json={}).json
            identity = state['interview_id']
            state, payload = self.submit(state, 'Java is a programming language.')
            self.assertTrue(state['is_follow_up'])
            self.assertEqual(self.client.post('/api/conversation/answer', json=payload).status_code, 202)
            state, _ = self.submit(state, 'It helped me build a project.')
            self.assertEqual(state['question_number'], 2)
            state, _ = self.submit(state, 'A class describes objects.')
            self.assertEqual(state['status'], 'completed')
            self.assertEqual(self.client.get('/final-scorecard').status_code, 200)
            self.assertEqual(self.client.get('/final-scorecard').status_code, 200)
            transcript = self.client.get('/api/conversation/transcript')
            self.assertEqual(transcript.status_code, 200)
            self.assertEqual(transcript.headers['Cache-Control'], 'no-store')
            self.assertEqual(len(transcript.json['messages']), 7)
            self.assertEqual(evaluate.call_count, 2)
            self.assertEqual(score.call_count, 1)
            other = run.app.test_client()
            with other.session_transaction() as session:
                session['voice_interview_id'] = identity
                session['voice_interview_token'] = 'wrong-token'
            self.assertEqual(other.get('/api/conversation').status_code, 404)
            self.assertEqual(other.get('/api/conversation/transcript').status_code, 404)
            journal = run.app.extensions['conversation'].journal
            with journal.sessions.begin() as db:
                self.assertEqual(db.get(VoiceSnapshot, identity).state['status'], 'completed')
                questions = db.scalars(select(Question).where(Question.interview_id == identity)).all()
                self.assertEqual(len(questions), 3)
                self.assertEqual(len(db.scalars(select(Answer).where(Answer.question_id.in_([q.id for q in questions]))).all()), 3)
                evaluations = db.scalars(select(Evaluation).join(Answer).where(Answer.question_id.in_([q.id for q in questions]))).all()
                self.assertEqual(len(evaluations), 2)
                self.assertTrue(all(evaluation.score == 7.5 for evaluation in evaluations))
                messages = db.scalars(select(ConversationMessage).where(ConversationMessage.interview_id == identity).order_by(ConversationMessage.sequence)).all()
                self.assertEqual([m.kind for m in messages], ['question', 'answered', 'follow_up', 'answered', 'question', 'answered', 'completion'])
                self.assertIsNotNone(db.scalar(select(Scorecard).where(Scorecard.interview_id == identity)))

    def test_concurrent_workers_and_early_end_reject_late_evaluation(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def delayed(question, answer):
            calls.append(answer)
            entered.set()
            release.wait(3)
            finished.set()
            return mock_evaluate(question, answer)
        service = run.app.extensions['conversation']
        peer = ConversationService(None, delayed, mock_scorecard, service.settings, journal=service.journal)
        try:
            with patch.object(run, 'evaluate_answer', side_effect=delayed):
                state = self.client.post('/api/conversation/start', json={}).json
                identity = state['interview_id']
                payload = dict(interview_id=identity, turn_id=state['turn_id'], request_id=uuid4().hex, answer='A response')
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda worker: worker.accept(identity, payload), (service, peer)))
                self.assertTrue(all(result['status'] == 'evaluating' for result in results))
                self.assertTrue(entered.wait(1))
                ended = self.client.post('/api/conversation/control', json={'action': 'end'}).json
                self.assertEqual(ended['status'], 'completed')
                release.set()
                self.assertTrue(finished.wait(1))
                peer.executor.shutdown(wait=True)
                recovered = peer.get(identity)
                self.assertEqual(recovered['status'], 'completed')
                self.assertEqual(recovered['interview_history'], [])
                self.assertTrue(recovered['final_scorecard']['insufficient_evidence'])
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.client.post('/api/conversation/answer', json=payload).status_code, 409)
        finally:
            release.set()
            peer.executor.shutdown(wait=True)

    def test_unanswered_is_recorded_without_evaluation(self):
        with patch.object(run, 'evaluate_answer', side_effect=AssertionError('Unexpected Gemini')):
            state = self.client.post('/api/conversation/start', json={}).json
            payload = dict(interview_id=state['interview_id'], turn_id=state['turn_id'], request_id=uuid4().hex, answer='', unanswered=True)
            self.assertEqual(self.client.post('/api/conversation/answer', json=payload).status_code, 202)
            next_state = self.wait()
            self.assertEqual(next_state['question_number'], 2)
            with run.app.extensions['conversation'].journal.sessions.begin() as db:
                question = db.scalar(select(Question).where(Question.interview_id == state['interview_id'], Question.sequence == 0, Question.parent_id.is_(None)))
                answer = db.scalar(select(Answer).where(Answer.question_id == question.id))
                self.assertEqual(answer.status, 'unanswered')
                self.assertIsNone(db.scalar(select(Evaluation).where(Evaluation.answer_id == answer.id)))

    def test_new_service_instance_recovers_interrupted_job(self):
        service = run.app.extensions['conversation']
        state = self.client.post('/api/conversation/start', json={}).json
        identity = state['interview_id']
        with service.journal.edit(identity) as data:
            request_id = uuid4().hex
            data.update(status='evaluating', active_request=request_id, draft='Saved answer')
            data['jobs'][request_id] = dict(turn_id=data['turn_id'], digest='a' * 64,
                                           status='running', stage='evaluation', updated_at=time.time() - 500)
        restarted = ConversationService(None, mock_evaluate, mock_scorecard, service.settings,
                                        journal=PostgresVoiceJournal(service.journal.sessions.kw['bind']))
        try:
            recovered = restarted.get(identity)
            self.assertEqual(recovered['status'], 'error')
            self.assertEqual(recovered['draft'], 'Saved answer')
            self.assertEqual(recovered['interview_history'], [])
            self.assertEqual(self.client.get('/api/conversation').json['status'], 'error')
        finally:
            restarted.executor.shutdown(wait=True)

    def test_manual_flow_uses_database_and_idempotent_forms(self):
        plan = {"questions": [
            {"question": "Explain Java", "skill": "Java", "type": "technical", "mock_follow_up": True},
            {"question": "Explain a class", "skill": "Java", "type": "technical"}]}
        with patch.object(run, 'extract_resume_text', return_value='Resume'), patch.object(run, 'generate_interview_plan', return_value=plan), patch.object(run, 'evaluate_answer', side_effect=mock_evaluate) as evaluate, patch.object(run, 'generate_final_scorecard', side_effect=mock_scorecard) as score:
            response = self.client.post('/', data={'candidate_name': 'Candidate', 'job_description': 'Java', 'resume': (io.BytesIO(b'pdf'), 'resume.pdf')})
            self.assertEqual(response.status_code, 302)
            page = self.client.get('/start-interview')
            token = re.search(rb'name="turn_id" value="([a-f0-9]+)"', page.data).group(1).decode()
            payload = {'turn_id': token, 'answer': 'A language'}
            first = self.client.post('/submit-answer', data=payload)
            self.assertEqual(first.location, '/follow-up')
            self.assertEqual(self.client.post('/submit-answer', data=payload).location, '/follow-up')
            self.assertEqual(evaluate.call_count, 1)
            page = self.client.get('/follow-up')
            token = re.search(rb'name="turn_id" value="([a-f0-9]+)"', page.data).group(1).decode()
            self.assertEqual(self.client.post('/submit-follow-up', data={'turn_id': token, 'answer': 'A project'}).location, '/start-interview')
            page = self.client.get('/start-interview')
            token = re.search(rb'name="turn_id" value="([a-f0-9]+)"', page.data).group(1).decode()
            self.assertEqual(self.client.post('/submit-answer', data={'turn_id': token, 'answer': 'A class'}).location, '/final-scorecard')
            self.assertEqual(self.client.get('/final-scorecard').status_code, 200)
            self.assertEqual(self.client.get('/api/conversation/transcript').status_code, 200)
            self.assertEqual(evaluate.call_count, 2)
            self.assertEqual(score.call_count, 1)

    def test_shared_session_and_mode_switch_keep_same_owned_interview(self):
        plan = {"questions": [{"question": "Explain Java", "skill": "Java", "type": "technical"}]}
        with patch.object(run, 'extract_resume_text', return_value='Resume'), patch.object(run, 'generate_interview_plan', return_value=plan):
            self.client.post('/', data={'candidate_name': 'Candidate', 'job_description': 'Java', 'resume': (io.BytesIO(b'pdf'), 'resume.pdf')})
            with self.client.session_transaction() as session:
                identity = session['manual_interview_id']
            cookie = self.client.get_cookie('session')
            worker_script = "import sys, run; c=run.app.test_client(); c.set_cookie('session', sys.stdin.read()); r=c.get('/api/conversation/transcript'); print(r.status_code)"
            worker = subprocess.run([sys.executable, '-c', worker_script], input=cookie.value, text=True, capture_output=True, timeout=20)
            self.assertEqual(worker.returncode, 0, worker.stderr)
            self.assertEqual(worker.stdout.strip(), '200')
            other_worker = run.app.test_client()
            other_worker.set_cookie('session', cookie.value)
            started = other_worker.post('/api/conversation/start', json={})
            self.assertEqual(started.status_code, 200)
            self.assertEqual(started.json['interview_id'], identity)
            self.assertEqual(other_worker.post('/api/conversation/control', json={'action': 'manual'}).status_code, 200)
            page = other_worker.get('/start-interview')
            self.assertIn(b'name="turn_id"', page.data)
            with other_worker.session_transaction() as session:
                self.assertEqual(session['manual_interview_id'], identity)
            self.assertEqual(other_worker.get('/api/conversation/transcript').status_code, 200)

    def test_manual_failed_evaluation_can_retry_saved_answer(self):
        plan = {"questions": [{"question": "Explain Java", "skill": "Java", "type": "technical"}]}
        with patch.object(run, 'extract_resume_text', return_value='Resume'), patch.object(run, 'generate_interview_plan', return_value=plan), patch.object(run, 'evaluate_answer', side_effect=[TimeoutError('mock timeout'), mock_evaluate({'question': 'Explain Java'}, 'Answer')]), patch.object(run, 'generate_final_scorecard', side_effect=mock_scorecard):
            self.client.post('/', data={'candidate_name': 'Candidate', 'job_description': 'Java', 'resume': (io.BytesIO(b'pdf'), 'resume.pdf')})
            page = self.client.get('/start-interview')
            turn = re.search(rb'name="turn_id" value="([a-f0-9]+)"', page.data).group(1).decode()
            self.assertEqual(self.client.post('/submit-answer', data={'turn_id': turn, 'answer': 'Saved answer'}).status_code, 503)
            page = self.client.get('/start-interview')
            self.assertIn(b'Saved answer</textarea>', page.data)
            self.assertEqual(self.client.post('/submit-answer', data={'turn_id': turn, 'answer': 'Saved answer'}).location, '/final-scorecard')

    def test_two_clients_submit_same_manual_turn_once(self):
        plan = {"questions": [{"question": "Explain Java", "skill": "Java", "type": "technical"}]}
        entered, release = threading.Event(), threading.Event()
        calls = []
        def evaluate(question, answer):
            calls.append(answer)
            entered.set()
            release.wait(3)
            return mock_evaluate(question, answer)
        with patch.object(run, 'extract_resume_text', return_value='Resume'), patch.object(run, 'generate_interview_plan', return_value=plan), patch.object(run, 'evaluate_answer', side_effect=evaluate), patch.object(run, 'generate_final_scorecard', side_effect=mock_scorecard):
            self.client.post('/', data={'candidate_name': 'Candidate', 'job_description': 'Java', 'resume': (io.BytesIO(b'pdf'), 'resume.pdf')})
            page = self.client.get('/start-interview')
            turn = re.search(rb'name="turn_id" value="([a-f0-9]+)"', page.data).group(1).decode()
            second = run.app.test_client()
            second.set_cookie('session', self.client.get_cookie('session').value)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(client.post, '/submit-answer', data={'turn_id': turn, 'answer': 'Java'}) for client in (self.client, second)]
                self.assertTrue(entered.wait(2))
                release.set()
                responses = [future.result(timeout=5) for future in futures]
            self.assertTrue(all(response.status_code == 302 for response in responses))
            self.assertEqual(calls, ['Java'])
