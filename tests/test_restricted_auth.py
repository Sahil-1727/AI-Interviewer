"""Restricted access tests use local sessions and no external services."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cachelib.file import FileSystemCache
from flask import Flask, session
from flask_session import Session
from werkzeug.security import generate_password_hash

from app.auth import init_auth


class RestrictedAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {'RESTRICTED_ACCESS': '1', 'APP_ENV': 'testing',
            'DATABASE_URL': 'postgresql://test-only',
            'RESTRICTED_ACCOUNTS_JSON': json.dumps({'candidate': generate_password_hash('correct-password')})})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = Flask(__name__, template_folder='../app/templates', static_folder='../app/static')
        self.app.secret_key = 'test-only-key'
        self.app.config.update(SESSION_TYPE='cachelib', SESSION_CACHELIB=FileSystemCache(cache_dir=self.directory.name))
        Session(self.app)
        self.app.jinja_env.globals['csrf_token'] = lambda: 'test-csrf'
        init_auth(self.app)
        self.app.add_url_rule('/', 'home', lambda: 'private interview')
        self.app.add_url_rule('/api/private', 'private', lambda: 'private api')
        self.client = self.app.test_client()

    def test_login_required_and_no_registration(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/api/private').status_code, 401)
        self.assertEqual(self.client.get('/register').status_code, 302)
        self.assertEqual(self.client.post('/login', data={'username': 'candidate', 'password': 'wrong'}).status_code, 401)
        self.assertEqual(self.client.post('/login', data={'username': 'candidate', 'password': 'correct-password'}).status_code, 302)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.client.post('/logout')
        self.assertEqual(self.client.get('/api/private').status_code, 401)

    def test_fails_closed_for_missing_configuration(self):
        with patch.dict(os.environ, {'RESTRICTED_ACCOUNTS_JSON': '{}'}):
            with self.assertRaises(RuntimeError):
                init_auth(Flask('misconfigured'))

    def test_password_rotation_invalidates_existing_session(self):
        self.client.post('/login', data={'username': 'candidate', 'password': 'correct-password'})
        self.assertEqual(self.client.get('/').status_code, 200)
        with patch.dict(os.environ, {'RESTRICTED_ACCOUNTS_JSON': json.dumps({'candidate': generate_password_hash('new-password')})}):
            self.assertEqual(self.client.get('/api/private').status_code, 401)
