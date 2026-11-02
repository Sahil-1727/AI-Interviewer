import copy
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from cachelib.file import FileSystemCache
import run
from app.services.conversation import ConversationService, ConversationError, mock_evaluate, mock_scorecard
from app.voice_config import DEFAULTS


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.evaluate = Mock(side_effect=mock_evaluate)
        self.scorecard = Mock(side_effect=mock_scorecard)
        self.service = ConversationService(self.directory.name + '/journal', self.evaluate, self.scorecard, DEFAULTS)
        old = run.app.extensions['conversation']
        run.app.extensions['conversation'] = self.service
        self.addCleanup(run.app.extensions.__setitem__, 'conversation', old)
        old_cache = run.app.session_interface.cache
        run.app.session_interface.cache = FileSystemCache(cache_dir=self.directory.name + '/sessions')
        self.addCleanup(setattr, run.app.session_interface, 'cache', old_cache)
        self.addCleanup(self.service.executor.shutdown, wait=True)
        self.client = run.app.test_client()
        self.seed(self.client)

    def seed(self, client, count=2, follow_up=True):
        with client.session_transaction() as session:
            session.clear()
            session.update(candidate_name='Test', interview_plan={'questions': [
                {'question': f'Question {i + 1}', 'skill': 'Python', 'type': 'technical', 'mock_follow_up': follow_up and i == 0}
                for i in range(count)]}, current_question=0, interview_history=[])

    def start(self):
        response = self.client.post('/api/conversation/start')
        self.assertEqual(response.status_code, 200)
        return response.json

    def payload(self, state, answer='A detailed example of my work.'):
        return dict(interview_id=state['interview_id'], turn_id=state['turn_id'], request_id=uuid4().hex, answer=answer)

    def wait(self, identity):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.service.get(identity)
            if state['status'] not in {'evaluating', 'scoring'}:
                return state
            time.sleep(.005)
        self.fail('Mock background job did not settle')

    def send(self, state):
        response = self.client.post('/api/conversation/answer', json=self.payload(state))
        self.assertEqual(response.status_code, 202, response.json)
        self.wait(state['interview_id'])
        return self.client.get('/api/conversation').json

    def test_whole_interview_with_follow_up_and_cached_report(self):
        with patch.object(run, 'evaluate_answer', side_effect=AssertionError('Unexpected Gemini')), patch.object(run, 'generate_final_scorecard', side_effect=AssertionError('Unexpected Gemini')):
            state = self.start()
            same = self.start()
            self.assertEqual(state['turn_id'], same['turn_id'])
            follow_up = self.send(state)
            self.assertTrue(follow_up['is_follow_up'])
            self.assertEqual(follow_up['question_number'], 1)
            next_question = self.send(follow_up)
            self.assertEqual(next_question['question_number'], 2)
            result = self.send(next_question)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(self.client.get(result['scorecard_url']).status_code, 200)
            self.assertEqual(self.client.get(result['scorecard_url']).status_code, 200)
        self.assertEqual(self.evaluate.call_count, 2, 'follow-up is evidence on original answer, as in manual mode')
        self.assertEqual(self.scorecard.call_count, 1)
        final = self.service.get(state['interview_id'])
        self.assertEqual(len(final['interview_history']), 2)
        self.assertIn('follow_up', final['interview_history'][0])
        self.assertEqual(final['current_question'], 2)

    def test_concurrent_same_and_different_keys_evaluate_only_once(self):
        entered, release = threading.Event(), threading.Event()
        def evaluate(question, answer):
            entered.set()
            release.wait(2)
            return mock_evaluate(question, answer)
        self.evaluate.side_effect = evaluate
        state = self.start()
        payload = self.payload(state)
        self.service.accept(state['interview_id'], payload)
        self.assertTrue(entered.wait(1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.accept(state['interview_id'], payload), range(4)))
        self.assertTrue(all(item['status'] == 'evaluating' for item in results))
        with self.assertRaises(ConversationError):
            self.service.accept(state['interview_id'], self.payload(state))
        with self.assertRaises(ConversationError):
            self.service.accept(state['interview_id'], dict(payload, answer='Changed answer'))
        release.set()
        result = self.wait(state['interview_id'])
        self.assertEqual(self.evaluate.call_count, 1)
        self.assertEqual(len(result['interview_history']), 1)
        # Duplicate after progression returns state without another evaluation.
        self.service.accept(state['interview_id'], payload)
        self.assertEqual(self.evaluate.call_count, 1)

    def test_empty_invalid_and_cross_session_submissions_are_rejected(self):
        state = self.start()
        for answer in ('', '   ', None, 'x' * 60001):
            self.assertEqual(self.client.post('/api/conversation/answer', json=self.payload(state, answer)).status_code, 400)
        self.assertEqual(self.client.post('/api/conversation/answer', json=[]).status_code, 400)
        self.assertEqual(self.client.post('/api/conversation/answer', json=dict(self.payload(state), interview_id=uuid4().hex)).status_code, 409)
        self.assertEqual(run.app.test_client().get('/api/conversation').status_code, 404)
        self.assertEqual(self.evaluate.call_count, 0)

    def test_gemini_error_preserves_answer_and_retry_is_explicit_idempotent(self):
        self.evaluate.side_effect = TimeoutError('mock timeout')
        state = self.start()
        payload = self.payload(state)
        self.client.post('/api/conversation/answer', json=payload)
        failed = self.wait(state['interview_id'])
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['draft'], payload['answer'])
        self.assertEqual(failed['current_question'], 0)
        self.assertEqual(failed['interview_history'], [])
        self.client.post('/api/conversation/answer', json=payload)
        self.assertEqual(self.evaluate.call_count, 1)
        self.evaluate.side_effect = mock_evaluate
        retry = self.payload(state)
        self.assertEqual(self.client.post('/api/conversation/retry', json=retry).status_code, 202)
        self.wait(state['interview_id'])
        self.client.post('/api/conversation/retry', json=retry)
        self.assertEqual(self.evaluate.call_count, 2)
        self.assertEqual(len(self.service.get(state['interview_id'])['interview_history']), 1)

    def test_scorecard_retry_does_not_reevaluate_last_answer(self):
        self.seed(self.client, count=1, follow_up=False)
        self.scorecard.side_effect = TimeoutError('mock scorecard timeout')
        state = self.start()
        result = self.send(state)
        self.assertEqual(result['status'], 'error')
        journal = self.service.get(state['interview_id'])
        self.assertEqual(journal['retry_stage'], 'scorecard')
        self.assertEqual(len(journal['interview_history']), 1)
        self.scorecard.side_effect = mock_scorecard
        self.client.post('/api/conversation/retry', json=self.payload(result, result['draft']))
        self.assertEqual(self.wait(state['interview_id'])['status'], 'completed')
        self.assertEqual(self.evaluate.call_count, 1)
        self.assertEqual(self.scorecard.call_count, 2)

    def test_pause_and_manual_fallback_preserve_progress(self):
        state = self.start()
        self.client.post('/api/conversation/control', json={'action': 'pause'})
        self.assertEqual(self.client.post('/api/conversation/answer', json=self.payload(state)).status_code, 409)
        self.client.post('/api/conversation/control', json={'action': 'resume'})
        next_state = self.send(state)
        self.assertTrue(next_state['is_follow_up'])
        self.assertEqual(self.client.post('/submit-follow-up', data={'answer': 'wrong mode'}).status_code, 409)
        self.assertEqual(self.client.post('/api/conversation/control', json={'action': 'manual'}).status_code, 200)
        response = self.client.post('/submit-follow-up', data={'answer': 'Manual follow-up'})
        self.assertEqual(response.location, '/start-interview')
        with self.client.session_transaction() as session:
            self.assertEqual(session['current_question'], 1)
            self.assertEqual(session['interview_history'][0]['follow_up']['answer'], 'Manual follow-up')

    def test_persistent_journal_restart_and_stale_job_recovery(self):
        state = self.start()
        with self.service.journal.edit(state['interview_id']) as data:
            key = uuid4().hex
            data.update(status='evaluating', active_request=key, draft='Saved answer')
            data['jobs'][key] = {'status': 'running', 'stage': 'evaluation', 'updated_at': time.time() - 500}
        recovered = self.service.get(state['interview_id'])
        self.assertEqual(recovered['status'], 'error')
        self.assertEqual(recovered['draft'], 'Saved answer')
        other = ConversationService(self.directory.name + '/journal', self.evaluate, self.scorecard, DEFAULTS)
        try:
            self.assertEqual(other.get(state['interview_id'])['status'], 'error')
            self.evaluate.assert_not_called()
        finally:
            other.executor.shutdown()

    def test_pause_during_evaluation_keeps_new_question_paused_and_end_stops_progression(self):
        entered, release = threading.Event(), threading.Event()
        def evaluate(question, answer):
            entered.set(); release.wait(2)
            return mock_evaluate(question, answer)
        self.evaluate.side_effect = evaluate
        state = self.start()
        self.client.post('/api/conversation/answer', json=self.payload(state))
        self.assertTrue(entered.wait(1))
        self.client.post('/api/conversation/control', json={'action': 'pause'})
        self.assertEqual(self.client.post('/api/conversation/control', json={'action': 'manual'}).status_code, 409)
        self.client.post('/api/conversation/control', json={'action': 'end'})
        release.set()
        result = self.wait(state['interview_id'])
        self.assertEqual(result['status'], 'ended')
        self.assertTrue(result['paused'])
        self.assertEqual(self.client.post('/api/conversation/control', json={'action': 'resume'}).status_code, 409)

    def test_mock_selection_is_server_side(self):
        with patch.dict(run.app.config, VOICE_MOCK_MODE=True):
            state = self.start()
            result = self.send(state)
            self.assertTrue(result['mock'])
            self.evaluate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
