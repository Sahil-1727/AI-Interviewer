"""Guard unnecessary database work on public resources; no external providers."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flask import Flask, session
from flask_sqlalchemy import SQLAlchemy

from app.services.shared_session import SharedSessionInterface
from app.request_timing import RequestTiming


class PublicResourceLatency(unittest.TestCase):
    def test_assets_and_readiness_skip_session_database_and_preserve_cookie(self):
        with TemporaryDirectory() as directory:
            Path(directory, 'demo.js').write_text('console.log("demo")')
            app = Flask(__name__, static_folder=directory, static_url_path='/static')
            app.secret_key = 'test-only'
            app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
            db = SQLAlchemy(app)
            interface = SharedSessionInterface(app, client=db, table='latency_sessions')
            app.session_interface = interface
            app.wsgi_app = RequestTiming(app.wsgi_app)
            app.add_url_rule('/health/ready', view_func=lambda: {'status': 'ready'})
            @app.get('/private')
            def private():
                return {'candidate': session.get('candidate')}
            client = app.test_client()
            client.set_cookie('session', 'existing-browser-session')
            with patch.object(interface, '_retrieve_session_data', side_effect=RuntimeError('SQL should not run')) as read, patch.object(interface, '_upsert_session') as write:
                for path in ('/static/demo.js', '/static/missing.js', '/health/ready'):
                    response = client.get(path)
                    self.assertIn(response.status_code, (200, 404))
                    self.assertNotIn('Set-Cookie', response.headers)
                    self.assertIn('app;dur=', response.headers['Server-Timing'])
                read.assert_not_called()
                write.assert_not_called()
            with patch.object(interface, '_retrieve_session_data', return_value={'candidate': 'saved'}) as read:
                self.assertEqual(client.get('/private').json['candidate'], 'saved')
                read.assert_called_once()
