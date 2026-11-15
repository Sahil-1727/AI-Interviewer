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


class SessionNavigationLatency(unittest.TestCase):
    def test_reads_skip_writes_but_changes_and_due_refresh_persist(self):
        from datetime import datetime, timedelta
        app = Flask(__name__)
        app.secret_key = 'test-only'
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', SESSION_REFRESH_INTERVAL_SECONDS=300)
        app.permanent_session_lifetime = timedelta(hours=1)
        db = SQLAlchemy(app)
        interface = SharedSessionInterface(app, client=db, table='navigation_sessions')
        app.session_interface = interface
        @app.post('/change')
        def change():
            session['value'] = session.get('value', 0) + 1
            return {'value': session['value']}
        @app.get('/read')
        def read():
            return {'value': session.get('value')}
        client = app.test_client()
        self.assertEqual(client.post('/change').json['value'], 1)
        with patch.object(interface, '_upsert_session', wraps=interface._upsert_session) as write:
            for _ in range(3):
                result = client.get('/read')
                self.assertEqual(result.json['value'], 1)
                self.assertNotIn('Set-Cookie', result.headers)
                self.assertIn('Cookie', result.headers['Vary'])
            write.assert_not_called()
            self.assertEqual(client.post('/change').json['value'], 2)
            self.assertEqual(write.call_count, 1)
        with app.app_context():
            record = db.session.query(interface.sql_session_model).one()
            record.expiry = datetime.utcnow() + timedelta(seconds=3200)
            db.session.commit()
        with patch.object(interface, '_upsert_session', wraps=interface._upsert_session) as write:
            self.assertEqual(client.get('/read').json['value'], 2)
            write.assert_called_once()
        with app.app_context():
            record = db.session.query(interface.sql_session_model).one()
            record.expiry = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
        self.assertIsNone(client.get('/read').json['value'])


class AssetCaching(unittest.TestCase):
    def test_version_changes_with_content_and_only_matching_versions_cache(self):
        from app.assets import init_assets
        from flask import url_for
        with TemporaryDirectory() as directory:
            path = Path(directory, 'demo.js')
            path.write_text('first')
            def make_app():
                app = Flask(__name__, static_folder=directory, static_url_path='/static')
                app.config['VERSIONED_ASSET_CACHE'] = True
                init_assets(app)
                return app
            first = make_app()
            with first.test_request_context():
                original = url_for('static', filename='demo.js')
            client = first.test_client()
            self.assertIn('immutable', client.get(original).headers['Cache-Control'])
            self.assertNotIn('immutable', client.get('/static/demo.js').headers['Cache-Control'])
            self.assertNotIn('immutable', client.get('/static/demo.js?v=wrong').headers['Cache-Control'])
            path.write_text('second')
            second = make_app()
            with second.test_request_context():
                updated = url_for('static', filename='demo.js')
            self.assertNotEqual(original, updated)
            second.config['VERSIONED_ASSET_CACHE'] = False
            self.assertNotIn('immutable', second.test_client().get(updated).headers['Cache-Control'])


class AssetFailureHandling(unittest.TestCase):
    def test_unreadable_asset_does_not_break_startup_or_get_immutable_cache(self):
        from app.assets import init_assets
        from flask import url_for
        with TemporaryDirectory() as directory:
            Path(directory, 'demo.js').write_text('test')
            app = Flask(__name__, static_folder=directory, static_url_path='/static')
            app.config['VERSIONED_ASSET_CACHE'] = True
            with patch.object(Path, 'read_bytes', side_effect=OSError('test failure')):
                init_assets(app)
            with app.test_request_context():
                self.assertEqual(url_for('static', filename='demo.js'), '/static/demo.js')
            self.assertNotIn('immutable', app.test_client().get('/static/demo.js').headers['Cache-Control'])
            self.assertEqual(app.test_client().get('/static/missing.js').status_code, 404)
