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
