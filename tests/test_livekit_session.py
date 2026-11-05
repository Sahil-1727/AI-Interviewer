"""Session admission tests; no LiveKit service or provider calls."""
import os
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
