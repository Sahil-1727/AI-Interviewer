import json
import os
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.security import generate_password_hash

from app.auth import init_auth
from app.security import init_security


class LiveKitAgentSecurityTests(unittest.TestCase):
    def test_service_endpoint_skips_browser_session_and_csrf_gates(self):
        env = {
            'APP_ENV': 'production',
            'DATABASE_URL': 'postgresql://test-only.invalid/test',
            'RESTRICTED_ACCOUNTS_JSON': json.dumps({'candidate': generate_password_hash('test-password')}),
            'CSRF_ENABLED': '1',
            'RATE_LIMIT_ENABLED': '0',
        }
        with patch.dict(os.environ, env):
            app = Flask('agent-security-test')
            app.secret_key = 'test-secret'
            app.add_url_rule('/api/conversation/livekit/agent/context', 'agent_context', lambda: 'ok', methods=['POST'])
            app.add_url_rule('/private', 'private', lambda: 'private', methods=['POST'])
            init_security(app)
            init_auth(app)
            client = app.test_client()

            # The real conversation route then enforces the service bearer
            # secret and signed interview ticket; these gates are browser-only.
            self.assertEqual(client.post('/api/conversation/livekit/agent/context').status_code, 200)
            self.assertEqual(client.post('/private').status_code, 403)


if __name__ == '__main__':
    unittest.main()
