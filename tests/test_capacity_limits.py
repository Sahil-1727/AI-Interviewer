"""No provider calls or production database: concurrency and shared admission tests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import create_engine, text

from app.capacity import init_capacity
from app.security import init_security
from app.services.request_limits import consume


class SharedLimits(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.directory.name) / 'limits.db'))
        with self.engine.begin() as connection:
            connection.execute(text('CREATE TABLE request_limits (client_hash TEXT, category TEXT, bucket BIGINT, count INTEGER, PRIMARY KEY(client_hash, category, bucket))'))

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def test_simultaneous_consumers_cannot_exceed_allowance(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            allowed = list(executor.map(lambda _: consume(self.engine, 'account:one', 'audio', 5, now=120)[0], range(24)))
        self.assertEqual(sum(allowed), 5)
        self.assertTrue(consume(self.engine, 'account:two', 'audio', 5, now=120)[0])
        self.assertTrue(consume(self.engine, 'account:one', 'audio', 5, now=180)[0])

    def make_app(self):
        app = Flask(__name__)
        app.secret_key = 'test-only'
        init_security(app)
        app.config.update(CSRF_ENABLED=False, RATE_LIMIT_ENABLED=True)
        init_capacity(app)
        app.add_url_rule('/api/synthesize', view_func=lambda: {'ok': True}, methods=['POST'])
        return app

    def test_account_limit_is_shared_across_apps_and_ips(self):
        with patch.dict(os.environ, DATABASE_URL='unused', RATE_LIMIT_AUDIO_PER_MINUTE='2'), patch('app.database.db', SimpleNamespace(engine=self.engine)):
            first, second = self.make_app().test_client(), self.make_app().test_client()
            for client in (first, second):
                with client.session_transaction() as session:
                    session['account_id'] = 'one'
            self.assertEqual(first.post('/api/synthesize').status_code, 200)
            self.assertEqual(second.post('/api/synthesize', environ_base={'REMOTE_ADDR': '192.0.2.2'}).status_code, 200)
            response = first.post('/api/synthesize', environ_base={'REMOTE_ADDR': '192.0.2.3'})
            self.assertEqual(response.status_code, 429)
            self.assertIn('Retry-After', response.headers)

    def test_polling_is_rate_limited_without_requiring_csrf(self):
        with patch.dict(os.environ, DATABASE_URL='unused', RATE_LIMIT_READ_PER_MINUTE='1'), patch('app.database.db', SimpleNamespace(engine=self.engine)):
            app = self.make_app()
            app.config['CSRF_ENABLED'] = True
            app.add_url_rule('/api/conversation', endpoint='conversation', view_func=lambda: {'ok': True})
            client = app.test_client()
            self.assertEqual(client.get('/api/conversation').status_code, 200)
            self.assertEqual(client.get('/api/conversation').status_code, 429)
            self.assertEqual(client.post('/api/synthesize').status_code, 403)

    def test_global_limit_and_database_failure(self):
        with patch.dict(os.environ, DATABASE_URL='unused', RATE_LIMIT_PROVIDER_GLOBAL_PER_MINUTE='1'), patch('app.database.db', SimpleNamespace(engine=self.engine)):
            first, second = self.make_app().test_client(), self.make_app().test_client()
            self.assertEqual(first.post('/api/synthesize').status_code, 200)
            self.assertEqual(second.post('/api/synthesize', environ_base={'REMOTE_ADDR': '192.0.2.2'}).status_code, 429)
            with patch('app.security.consume', side_effect=RuntimeError('private credentials')):
                response = first.post('/api/synthesize')
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('private', response.get_data(as_text=True))


class Concurrency(unittest.TestCase):
    def test_overload_rejects_promptly_and_control_remains_available(self):
        entered, release = Event(), Event()
        app = Flask(__name__)
        with patch.dict(os.environ, HTTP_PROVIDER_CONCURRENCY='1'):
            init_capacity(app)
        @app.post('/api/synthesize')
        def synthesize():
            entered.set()
            release.wait(5)
            return {'ok': True}
        app.add_url_rule('/api/conversation/control', view_func=lambda: {'ok': True}, methods=['POST'])
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: app.test_client().post('/api/synthesize').status_code)
            try:
                self.assertTrue(entered.wait(2))
                client = app.test_client()
                self.assertEqual(client.post('/api/synthesize').status_code, 503)
                self.assertEqual(client.post('/api/conversation/control').status_code, 200)
            finally:
                release.set()
            self.assertEqual(future.result(), 200)
        self.assertEqual(app.test_client().post('/api/synthesize').status_code, 200)

    def test_exception_releases_capacity(self):
        app = Flask(__name__)
        app.testing = True
        with patch.dict(os.environ, HTTP_PROVIDER_CONCURRENCY='1'):
            init_capacity(app)
        @app.post('/api/synthesize')
        def fail():
            raise RuntimeError('test')
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                app.test_client().post('/api/synthesize')

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires isolated PostgreSQL database')
class PostgreSQLLimits(unittest.TestCase):
    def test_atomic_limit_across_independent_connection_pools(self):
        from uuid import uuid4
        engines = [create_engine(os.environ['TEST_DATABASE_URL']) for _ in range(2)]
        identity = 'scaling-test:' + uuid4().hex
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda i: consume(engines[i % 2], identity, 'audio', 7)[0], range(30)))
            self.assertEqual(sum(results), 7)
        finally:
            for engine in engines:
                engine.dispose()
