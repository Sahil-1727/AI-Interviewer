"""Session admission tests; no LiveKit service or provider calls."""
import os
import time
import unittest
from unittest.mock import Mock, patch

import jwt
import json
from contextlib import contextmanager
from flask import Flask, session

from app.routes.conversation import conversation_bp
from app.services.conversation import ConversationError


class LiveKitSessionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only-session-secret'
        self.service = Mock()
        self.service.get.return_value = {'status': 'ready', 'paused': False, 'end_requested': False}
        self.saved_state = {'status': 'ready', 'paused': False, 'end_requested': False}
        @contextmanager
        def edit(_identity):
            yield self.saved_state
        self.service.journal.edit.side_effect = edit
        self.app.extensions['conversation'] = self.service
        self.app.register_blueprint(conversation_bp)
        self.client = self.app.test_client()
        self.env = patch.dict(os.environ, {'LIVEKIT_ENABLED': '1', 'LIVEKIT_URL': 'wss://example.test',
            'LIVEKIT_API_KEY': 'test-key', 'LIVEKIT_API_SECRET': 'test-secret-test-secret-test-secret',
            'LIVEKIT_AGENT_SHARED_SECRET': 'worker-secret-worker-secret-worker-secret'}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def own_session(self):
        with self.client.session_transaction() as data:
            data['voice_interview_id'] = 'a' * 32
            data['voice_interview_token'] = 'private-owner-capability'

    def test_owner_gets_scoped_short_lived_token(self):
        self.own_session()
        response = self.client.post('/api/conversation/livekit/session')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        claims = jwt.decode(response.json['token'], 'test-secret-test-secret-test-secret', algorithms=['HS256'], options={'verify_aud': False})
        self.assertTrue(claims['video']['room'].startswith('interview-' + 'a' * 32 + '-'))
        self.assertLessEqual(claims['exp'] - claims['nbf'], 301)
        self.assertNotIn('private-owner-capability', str(claims))
        self.service.authorize.assert_called_once()

    def test_agent_requires_secret_and_scoped_ticket(self):
        self.own_session()
        token = self.client.post('/api/conversation/livekit/session').json['token']
        claims = jwt.decode(token, 'test-secret-test-secret-test-secret', algorithms=['HS256'], options={'verify_aud': False})
        ticket = json.loads(claims['roomConfig']['agents'][0]['metadata'])['ticket']
        path = '/api/conversation/livekit/agent/state'
        self.assertEqual(self.client.post(path, json={'ticket': ticket}).status_code, 403)
        headers = {'Authorization': 'Bearer worker-secret-worker-secret-worker-secret'}
        self.assertEqual(self.client.post(path, json={'ticket': 'invalid'}, headers=headers).status_code, 403)
        self.service.get.return_value = {'id': 'a' * 32, 'turn_id': 'b' * 32, 'status': 'ready',
            'paused': False, 'interview_plan': {'questions': []}, 'jobs': {}, 'mock': False,
            'current_question': 0, 'end_requested': False}
        self.assertEqual(self.client.post(path, json={'ticket': ticket}, headers=headers).status_code, 200)

    def test_no_session_or_ended_interview_cannot_join(self):
        self.assertEqual(self.client.post('/api/conversation/livekit/session').status_code, 404)
        self.own_session()
        self.service.get.return_value = {'status': 'completed'}
        self.assertEqual(self.client.post('/api/conversation/livekit/session').status_code, 409)

    def test_disabled_and_missing_configuration_fail_closed(self):
        self.own_session()
        with patch.dict(os.environ, {'LIVEKIT_ENABLED': '0'}):
            self.assertEqual(self.client.post('/api/conversation/livekit/session').status_code, 404)
        with patch.dict(os.environ, {'LIVEKIT_API_KEY': ''}):
            self.assertEqual(self.client.post('/api/conversation/livekit/session').status_code, 503)

    def test_invalid_ownership_never_issues_token(self):
        self.own_session()
        self.service.authorize.side_effect = ConversationError('Interview not found.', 404)
        self.assertEqual(self.client.post('/api/conversation/livekit/session').status_code, 404)
        self.service.get.assert_not_called()


class LiveKitInterviewFlowTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from app.services.conversation import ConversationService, mock_evaluate, mock_scorecard
        from app.voice_config import DEFAULTS
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = ConversationService(self.directory.name, mock_evaluate, mock_scorecard, DEFAULTS)
        self.addCleanup(self.service.executor.shutdown, wait=True)
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only-secret'
        self.app.extensions['conversation'] = self.service
        self.app.register_blueprint(conversation_bp)
        self.client = self.app.test_client()
        self.env = patch.dict(os.environ, {'LIVEKIT_ENABLED': '1', 'LIVEKIT_URL': 'wss://example.test',
            'LIVEKIT_API_KEY': 'test-key', 'LIVEKIT_API_SECRET': 'test-secret-test-secret-test-secret',
            'LIVEKIT_AGENT_SHARED_SECRET': 'worker-secret-worker-secret-worker-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)
        with self.client.session_transaction() as data:
            data.update(candidate_name='Candidate', interview_plan={'questions': [
                {'question': 'Explain Java.', 'skill': 'Java', 'type': 'technical', 'mock_follow_up': True},
                {'question': 'Explain classes.', 'skill': 'Java', 'type': 'technical'}]},
                current_question=0, interview_history=[])
        self.client.post('/api/conversation/start')
        token = self.client.post('/api/conversation/livekit/session').json['token']
        claims = jwt.decode(token, 'test-secret-test-secret-test-secret', algorithms=['HS256'], options={'verify_aud': False})
        self.ticket = json.loads(claims['roomConfig']['agents'][0]['metadata'])['ticket']
        self.headers = {'Authorization': 'Bearer worker-secret-worker-secret-worker-secret'}

    def call(self, action, **values):
        return self.client.post('/api/conversation/livekit/agent/' + action,
                                json={'ticket': self.ticket, **values}, headers=self.headers)

    def wait_ready(self):
        import time
        for _ in range(300):
            state = self.call('state').json
            if state['status'] not in {'evaluating', 'scoring', 'ending'}:
                return state
            time.sleep(.01)
        self.fail('Interview did not progress')

    def test_complete_mocked_interview_with_follow_up_and_scorecard(self):
        from uuid import uuid4
        state = self.call('state').json
        self.assertEqual(state['question']['question'], 'Explain Java.')
        for answer in ['Java is a programming language.', 'I used it in a project.', 'Classes define objects.']:
            payload = {'turn_id': state['turn_id'], 'request_id': uuid4().hex, 'answer': answer}
            self.assertEqual(self.call('answer', **payload).status_code, 202)
            state = self.wait_ready()
            self.assertEqual(self.call('answer', **payload).status_code, 202)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['scorecard_url'], '/final-scorecard')

    def test_new_room_invalidates_previous_agent(self):
        from uuid import uuid4
        old_ticket = self.ticket
        state = self.call('state').json
        token = self.client.post('/api/conversation/livekit/session').json['token']
        claims = jwt.decode(token, 'test-secret-test-secret-test-secret', algorithms=['HS256'], options={'verify_aud': False})
        self.ticket = json.loads(claims['roomConfig']['agents'][0]['metadata'])['ticket']
        self.assertNotEqual(self.ticket, old_ticket)
        self.assertEqual(self.client.post('/api/conversation/livekit/agent/answer', headers=self.headers,
            json={'ticket': old_ticket, 'turn_id': state['turn_id'], 'request_id': uuid4().hex,
                  'answer': 'stale answer'}).status_code, 409)
        self.assertEqual(self.call('answer', turn_id=state['turn_id'], request_id=uuid4().hex,
                                   answer='current answer').status_code, 202)

    def test_context_is_scoped_to_current_agent_and_not_public(self):
        response = self.call('context')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.json['interview_plan']['questions'][0]['question'], 'Explain Java.')
        self.assertNotIn('jobs', response.json)
        self.assertNotIn('owner_account', response.json)
        self.assertEqual(self.client.post('/api/conversation/livekit/agent/context',
            json={'ticket': self.ticket}).status_code, 403)
        old_ticket = self.ticket
        self.client.post('/api/conversation/livekit/session')
        self.assertEqual(self.client.post('/api/conversation/livekit/agent/context', headers=self.headers,
            json={'ticket': old_ticket}).status_code, 409)

    def test_event_is_durable_idempotent_and_rejects_changed_payload(self):
        from uuid import uuid4
        state = self.call('state').json
        event = {'event_id': uuid4().hex, 'turn_id': state['turn_id'],
                 'kind': 'candidate', 'text': 'My original answer.'}
        first, repeat = self.call('event', **event), self.call('event', **event)
        self.assertEqual((first.status_code, repeat.status_code), (202, 202))
        self.assertEqual(first.json, repeat.json)
        self.assertEqual(self.call('event', **{**event, 'text': 'Changed'}).status_code, 409)
        identity = state['interview_id']
        self.assertEqual(len(self.service.get(identity)['livekit_events']), 1)
        self.assertEqual(self.client.post('/api/conversation/livekit/agent/event', json=event).status_code, 403)
        old_ticket = self.ticket
        self.client.post('/api/conversation/livekit/session')
        self.assertEqual(self.client.post('/api/conversation/livekit/agent/event', headers=self.headers,
            json={'ticket': old_ticket, **event}).status_code, 409)

    def test_concurrent_retries_append_one_event(self):
        from concurrent.futures import ThreadPoolExecutor
        from uuid import uuid4
        state = self.call('state').json
        identity = state['interview_id']
        generation = self.service.get(identity)['livekit_generation']
        event = {'event_id': uuid4().hex, 'turn_id': state['turn_id'],
                 'kind': 'candidate', 'text': 'A single answer.'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.append_livekit_event(identity, generation, event), range(8)))
        self.assertEqual({item['sequence'] for item in results}, {0})
        self.assertEqual(len(self.service.get(identity)['livekit_events']), 1)

    def test_expired_agent_cannot_append_an_event(self):
        from datetime import datetime, timedelta, timezone
        from uuid import uuid4
        state = self.call('state').json
        with self.service.journal.edit(state['interview_id']) as current:
            current['livekit_expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        result = self.call('event', event_id=uuid4().hex, turn_id=state['turn_id'],
                           kind='candidate', text='An answer.')
        self.assertEqual(result.status_code, 410)
        self.assertNotIn('livekit_events', self.service.get(state['interview_id']))

    def test_manual_handoff_is_allowed_before_livekit_transcript_exists(self):
        state = self.call('state').json
        self.assertEqual(self.call('context').status_code, 200)
        result = self.service.control(state['interview_id'], 'manual')
        self.assertEqual(result['status'], 'manual')
        self.assertFalse(self.service.get(state['interview_id'])['agent_led'])

    def test_manual_handoff_refuses_to_drop_saved_livekit_speech(self):
        from uuid import uuid4
        state = self.call('state').json
        self.assertEqual(self.call('context').status_code, 200)
        self.assertEqual(self.call('event', event_id=uuid4().hex, turn_id=state['turn_id'],
                                   kind='candidate', text='I am doing well, thank you.').status_code, 202)
        with self.assertRaises(ConversationError):
            self.service.control(state['interview_id'], 'manual')

    def test_agent_led_transcript_is_evaluated_and_scorecard_is_built(self):
        from uuid import uuid4
        state = self.call('state').json
        self.assertEqual(self.call('context').status_code, 200)
        for role, text in [('interviewer', 'Explain Java.'),
                           ('candidate', 'I built a small Java application.'),
                           ('interviewer', 'Can you share an example?'),
                           ('candidate', 'I used classes in that application.'),
                           ('interviewer', 'Explain classes.'),
                           ('candidate', 'A class describes objects and behavior.')]:
            saved = self.call('event', event_id=uuid4().hex, turn_id=uuid4().hex,
                              kind=role, text=text)
            self.assertEqual(saved.status_code, 202)
        response = self.call('finalize')
        self.assertEqual(response.status_code, 202)
        for _ in range(300):
            state = self.call('state').json
            if state['status'] in {'completed', 'error'}:
                break
            time.sleep(.01)
        self.assertEqual(state['status'], 'completed')
        snapshot = self.service.get(state['interview_id'])
        self.assertEqual([row['question'] for row in snapshot['interview_history']],
                         ['Explain Java.', 'Can you share an example?', 'Explain classes.'])
        self.assertEqual([row['type'] for row in snapshot['interview_history']],
                         ['technical', 'follow_up', 'technical'])
        self.assertTrue(all(row.get('evaluation') for row in snapshot['interview_history']))
        self.assertTrue(snapshot['final_scorecard'])
        self.assertEqual(snapshot['status'], 'completed')
        self.assertFalse(snapshot['early_ended'])

    def test_finalization_retries_only_the_failed_gemini_evaluation(self):
        from uuid import uuid4
        from unittest.mock import Mock
        from google.genai.errors import ServerError
        from app.services.conversation import mock_evaluate

        state = self.call('state').json
        self.assertEqual(self.call('context').status_code, 200)
        evaluation = mock_evaluate({'question': 'Explain Java.', 'skill': 'Java', 'type': 'technical'},
                                  'Java is an object-oriented language.')
        evaluator = Mock(side_effect=[ServerError(503, {'error': {'message': 'temporary'}}), evaluation])
        self.service.evaluator = evaluator
        for kind, text in [('interviewer', 'Explain Java.'),
                           ('candidate', 'Java is an object-oriented language.')]:
            self.assertEqual(self.call('event', event_id=uuid4().hex, turn_id=state['turn_id'],
                                       kind=kind, text=text).status_code, 202)

        finalize_response = self.call('finalize')
        self.assertEqual(finalize_response.status_code, 202, finalize_response.json)
        for _ in range(300):
            state = self.call('state').json
            if state['status'] == 'error':
                break
            time.sleep(.01)
        self.assertEqual(state['finalization_error_stage'], 'evaluate_answer')
        self.assertEqual(evaluator.call_count, 1)

        finalize_response = self.call('finalize')
        self.assertEqual(finalize_response.status_code, 202, finalize_response.json)
        for _ in range(300):
            state = self.call('state').json
            if state['status'] in {'completed', 'error'}:
                break
            time.sleep(.01)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(evaluator.call_count, 2)

    def test_finalization_evaluates_independent_answers_concurrently(self):
        from threading import Barrier, Lock
        from uuid import uuid4
        from app.services.conversation import mock_evaluate

        state = self.call('state').json
        self.assertEqual(self.call('context').status_code, 200)
        self.service.settings = dict(self.service.settings)
        self.service.settings['LIVEKIT_FINALIZE_EVALUATION_CONCURRENCY'] = 2
        barrier = Barrier(2)
        active_lock = Lock()
        active = 0
        peak_active = 0

        def evaluator(question, answer):
            nonlocal active, peak_active
            with active_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                barrier.wait(timeout=2)
                return mock_evaluate(question, answer)
            finally:
                with active_lock:
                    active -= 1

        self.service.evaluator = evaluator
        for question, answer in [('Explain Java.', 'Java is an object-oriented language.'),
                                 ('Explain classes.', 'A class describes an object.')]:
            self.assertEqual(self.call('event', event_id=uuid4().hex, turn_id=state['turn_id'],
                                       kind='interviewer', text=question).status_code, 202)
            self.assertEqual(self.call('event', event_id=uuid4().hex, turn_id=state['turn_id'],
                                       kind='candidate', text=answer).status_code, 202)

        finalize_response = self.call('finalize')
        self.assertEqual(finalize_response.status_code, 202, finalize_response.json)
        for _ in range(500):
            state = self.call('state').json
            if state['status'] in {'completed', 'error'}:
                break
            time.sleep(.01)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(peak_active, 2)

    def test_livekit_repeat_command_is_not_evaluated_as_an_answer(self):
        state = {'interview_plan': {'questions': [{'question': 'Explain Java.', 'skill': 'Java'}]},
                 'current_question': 0, 'livekit_events': [
                     {'kind': 'interviewer', 'text': 'Explain Java.'},
                     {'kind': 'candidate', 'text': 'Could you repeat the question?'},
                     {'kind': 'interviewer', 'text': 'Explain Java.'},
                     {'kind': 'candidate', 'text': 'Java is an object-oriented language.'},
                 ]}
        rows, answered, next_question, pending = self.service.reconcile_livekit_transcript(state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['answer'], 'Java is an object-oriented language.')
        self.assertEqual(answered, {0})
        self.assertEqual(next_question, 0)
        self.assertIsNone(pending)
