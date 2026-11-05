"""Quota and ownership checks; requires a dedicated migrated test database."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires dedicated PostgreSQL test database')
class RestrictedPostgresTests(unittest.TestCase):
    def test_production_requires_login_and_disables_legacy_voice(self):
        import json
        import subprocess
        import sys
        from werkzeug.security import generate_password_hash

        env = os.environ.copy()
        env.update(APP_ENV='production', DATABASE_URL=os.environ['TEST_DATABASE_URL'],
                   SECRET_KEY='test-only-session-secret-at-least-32-characters', SESSION_COOKIE_SECURE='1',
                   LIVEKIT_ENABLED='1', CSRF_ENABLED='1', RATE_LIMIT_ENABLED='1',
                   RESTRICTED_ACCOUNTS_JSON=json.dumps({'production-test': generate_password_hash('test-password')}))
        script = '''
import re
import run
c = run.app.test_client()
assert c.get('/').status_code == 302
page = c.get('/login')
token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
assert c.post('/login', data={'username': 'production-test', 'password': 'test-password', 'csrf_token': token}).status_code == 302
assert c.get('/').status_code == 200
assert c.get('/api/voice/stream').status_code == 404
'''
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_account_cannot_issue_token_for_another_accounts_interview(self):
        import json
        import run
        from flask import Flask
        from flask_sqlalchemy import SQLAlchemy
        from werkzeug.security import generate_password_hash
        from app.auth import init_auth
        from app.routes.conversation import conversation_bp
        from app.services.shared_session import SharedSessionInterface

        alice_name, bob_name = 'alice-' + uuid4().hex, 'bob-' + uuid4().hex
        env = {'RESTRICTED_ACCESS': '1', 'APP_ENV': 'testing', 'DATABASE_URL': os.environ['TEST_DATABASE_URL'],
               'LIVEKIT_ENABLED': '1', 'LIVEKIT_URL': 'wss://example.test', 'LIVEKIT_API_KEY': 'test-key',
               'LIVEKIT_API_SECRET': 'test-secret-test-secret-test-secret',
               'LIVEKIT_AGENT_SHARED_SECRET': 'worker-secret-worker-secret-worker-secret',
               'INTERVIEW_MAX_GLOBAL_PER_DAY': '1000', 'INTERVIEW_MAX_ACTIVE': '1000',
               'RESTRICTED_ACCOUNTS_JSON': json.dumps({name: generate_password_hash('test-password') for name in (alice_name, bob_name)})}
        with patch.dict(os.environ, env):
            app = Flask('restricted-token-test', template_folder='../app/templates', static_folder='../app/static')
            app.secret_key = 'test-only-session-key'
            app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['TEST_DATABASE_URL']
            client_db = SQLAlchemy(app)
            app.session_interface = SharedSessionInterface(app, client=client_db, table='web_sessions', permanent=False)
            app.extensions['conversation'] = run.app.extensions['conversation']
            app.jinja_env.globals['csrf_token'] = lambda: 'test'
            init_auth(app)
            app.register_blueprint(conversation_bp)
            app.add_url_rule('/', 'home', lambda: 'private')
            alice, bob = app.test_client(), app.test_client()
            for client, name in ((alice, alice_name), (bob, bob_name)):
                self.assertEqual(client.post('/login', data={'username': name, 'password': 'test-password'}).status_code, 302)
            with alice.session_transaction() as session:
                session.update(candidate_name='Test', evaluation_context={},
                    interview_plan={'questions': [{'question': 'Explain Java', 'skill': 'Java', 'type': 'technical'}]},
                    current_question=0, interview_history=[])
            self.assertEqual(alice.post('/api/conversation/start', json={}).status_code, 200)
            with alice.session_transaction() as session:
                identity, token = session['voice_interview_id'], session['voice_interview_token']
            with bob.session_transaction() as session:
                session['voice_interview_id'], session['voice_interview_token'] = identity, token
            self.assertEqual(bob.post('/api/conversation/livekit/session', json={}).status_code, 404)
            with patch('app.services.interview_quota.db', client_db):
                issued = alice.post('/api/conversation/livekit/session', json={})
                self.assertEqual(issued.status_code, 200)
                import jwt
                claims = jwt.decode(issued.json['token'], env['LIVEKIT_API_SECRET'], algorithms=['HS256'], options={'verify_aud': False})
                ticket = json.loads(claims['roomConfig']['agents'][0]['metadata'])['ticket']
                rotated = json.dumps({alice_name: generate_password_hash('new-password'), bob_name: generate_password_hash('test-password')})
                with patch.dict(os.environ, {'RESTRICTED_ACCOUNTS_JSON': rotated}):
                    response = alice.post('/api/conversation/livekit/agent/state', json={'ticket': ticket},
                        headers={'Authorization': 'Bearer ' + env['LIVEKIT_AGENT_SHARED_SECRET']})
                    self.assertEqual(response.status_code, 403)

    def test_login_rotates_existing_shared_session_without_losing_login(self):
        import json
        from flask import Flask
        from werkzeug.security import generate_password_hash
        from app.auth import init_auth
        from flask_sqlalchemy import SQLAlchemy
        from app.services.shared_session import SharedSessionInterface

        env = {'RESTRICTED_ACCESS': '1', 'APP_ENV': 'testing', 'DATABASE_URL': os.environ['TEST_DATABASE_URL'],
               'RESTRICTED_ACCOUNTS_JSON': json.dumps({'candidate': generate_password_hash('test-password')})}
        with patch.dict(os.environ, env):
            app = Flask('restricted-postgres-test', template_folder='../app/templates', static_folder='../app/static')
            app.secret_key = 'test-only-session-key'
            app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['TEST_DATABASE_URL']
            client_db = SQLAlchemy(app)
            app.session_interface = SharedSessionInterface(app, client=client_db, table='web_sessions', permanent=False)
            app.jinja_env.globals['csrf_token'] = lambda: 'test'
            init_auth(app)
            app.add_url_rule('/', 'home', lambda: 'private')
            client = app.test_client()
            with client.session_transaction() as data:
                data['existing'] = 'session'
            self.assertEqual(client.post('/login', data={'username': 'candidate', 'password': 'test-password'}).status_code, 302)
            self.assertEqual(client.get('/').status_code, 200)
            with client.session_transaction() as data:
                self.assertEqual(data.get('account_name'), 'candidate')
                self.assertNotIn('existing', data)

    def test_user_daily_limit_counts_completed_interviews(self):
        import run
        from app.services.interview_quota import reserve, finish, QuotaError
        journal = run.app.extensions['conversation'].journal
        account = 'test-' + uuid4().hex
        seed = {'candidate_name': 'Test', 'account_name': account, 'evaluation_context': {},
                'interview_plan': {'questions': [{'question': 'Explain Java', 'skill': 'Java', 'type': 'technical'}]},
                'current_question': 0, 'interview_history': []}
        with run.app.app_context():
            first, _ = journal.initialize(seed, False)
            second, _ = journal.initialize(seed, False)
            with patch.dict(os.environ, {'INTERVIEW_MAX_PER_USER_PER_DAY': '1', 'INTERVIEW_MAX_GLOBAL_PER_DAY': '1000', 'INTERVIEW_MAX_ACTIVE': '1000'}):
                reserve(first, account)
                finish(first)
                with self.assertRaises(QuotaError):
                    reserve(second, account)

    def test_limits_serialize_concurrent_admission_and_expiry(self):
        import run
        from sqlalchemy import select, update, func
        from app.models.interview import InterviewUsage
        from app.services.interview_quota import reserve, active, QuotaError

        journal = run.app.extensions['conversation'].journal
        account = 'test-' + uuid4().hex
        seed = {'candidate_name': 'Test', 'account_name': account, 'evaluation_context': {},
                'interview_plan': {'questions': [{'question': 'Explain Java', 'skill': 'Java', 'type': 'technical'}]},
                'current_question': 0, 'interview_history': []}
        with run.app.app_context():
            first, token = journal.initialize(seed, False)
            second, _ = journal.initialize(seed, False)
            journal.authorize(first, token, account)
            from app.services.conversation import ConversationError
            with self.assertRaises(ConversationError):
                journal.authorize(first, token, 'another-account')

        def admit(identity):
            with run.app.app_context():
                try:
                    reserve(identity, account)
                    return 'accepted', identity
                except QuotaError:
                    return 'rejected', identity
        with patch.dict(os.environ, {'INTERVIEW_MAX_PER_USER_PER_DAY': '3', 'INTERVIEW_MAX_GLOBAL_PER_DAY': '1000', 'INTERVIEW_MAX_ACTIVE': '1000'}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(admit, [first, second]))
            self.assertEqual(sorted(result[0] for result in results), ['accepted', 'rejected'])
            accepted = next(identity for status, identity in results if status == 'accepted')
            with run.app.app_context(), journal.sessions.begin() as db_session:
                db_session.execute(update(InterviewUsage).where(InterviewUsage.interview_id == accepted).values(expires_at=func.now() - timedelta(seconds=1)))
            with run.app.app_context():
                self.assertFalse(active(accepted))
                with self.assertRaises(QuotaError):
                    reserve(accepted, account)
