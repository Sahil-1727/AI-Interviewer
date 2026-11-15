"""Exercise CSRF without provider calls."""
import re
import unittest
from unittest.mock import patch

import run


class RequestSecurityTests(unittest.TestCase):
    def test_csrf_required_and_valid_token_reaches_route(self):
        with patch.dict(run.app.config, CSRF_ENABLED=True, RATE_LIMIT_ENABLED=False, TESTING=True):
            client = run.app.test_client()
            page = client.get('/')
            token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
            self.assertEqual(client.post('/api/conversation/start', json={}).status_code, 403)
            self.assertEqual(client.post('/api/conversation/start', json={}, headers={'X-CSRF-Token': 'invalid'}).status_code, 403)
            self.assertEqual(client.post('/api/conversation/start', json={}, headers={'X-CSRF-Token': token}).status_code, 400)
            self.assertEqual(client.post('/', data={'csrf_token': token}).status_code, 400)

    def test_browser_headers_protect_private_pages_and_allow_local_microphone(self):
        with patch.dict(run.app.config, TESTING=True, RATE_LIMIT_ENABLED=False):
            response = run.app.test_client().get('/')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn('microphone=(self)', response.headers['Permissions-Policy'])
            asset = run.app.test_client().get('/static/js/ui.js')
            self.assertNotEqual(asset.headers['Cache-Control'], 'no-store')
            asset.close()

    def test_multipart_part_limit_rejects_resource_exhaustion(self):
        from flask import Flask, request
        from app.security import init_security
        from werkzeug.datastructures import MultiDict
        app = Flask(__name__)
        app.secret_key = 'test-only'
        init_security(app)
        app.config.update(CSRF_ENABLED=False, RATE_LIMIT_ENABLED=False)
        @app.post('/upload')
        def upload():
            return {'count': len(request.form)}
        client = app.test_client()
        self.assertEqual(client.post('/upload', data={'field': 'value'}, content_type='multipart/form-data').status_code, 200)
        excessive = MultiDict((f'field{i}', 'x') for i in range(101))
        self.assertEqual(client.post('/upload', data=excessive, content_type='multipart/form-data').status_code, 413)
