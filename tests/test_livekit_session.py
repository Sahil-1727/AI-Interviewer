"""Session admission tests; no LiveKit service or provider calls."""
import os
import unittest
from unittest.mock import Mock, patch

import jwt
from flask import Flask, session

from app.routes.conversation import conversation_bp
from app.services.conversation import ConversationError


class LiveKitSessionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only-session-secret'
        self.service = Mock()
        self.service.get.return_value = {'status': 'ready', 'paused': False, 'end_requested': False}
        self.app.extensions['conversation'] = self.service
        self.app.register_blueprint(conversation_bp)
        self.client = self.app.test_client()
        self.env = patch.dict(os.environ, {'LIVEKIT_ENABLED': '1', 'LIVEKIT_URL': 'wss://example.test',
            'LIVEKIT_API_KEY': 'test-key', 'LIVEKIT_API_SECRET': 'test-secret-test-secret-test-secret'}, clear=False)
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
        self.assertEqual(claims['video']['room'], 'interview-' + 'a' * 32)
        self.assertLessEqual(claims['exp'] - claims['nbf'], 301)
        self.assertNotIn('private-owner-capability', str(claims))
        self.service.authorize.assert_called_once()

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
