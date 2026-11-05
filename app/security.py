"""Browser CSRF validation and PostgreSQL-backed request limits."""
import hashlib
import hmac
import os
import secrets
import time

from flask import jsonify, request, session
from sqlalchemy import text


def csrf_token():
    value = session.get('_csrf_token')
    if not value:
        value = secrets.token_urlsafe(32)
        session['_csrf_token'] = value
    return value


def init_security(app):
    app.config['CSRF_ENABLED'] = os.getenv('CSRF_ENABLED', '1') != '0'
    app.config['RATE_LIMIT_ENABLED'] = os.getenv('RATE_LIMIT_ENABLED', '1') != '0'

    @app.context_processor
    def csrf_context():
        return {'csrf_token': csrf_token}

    @app.before_request
    def protect_state_changes():
        # Worker calls are authenticated by a constant-time shared-secret
        # check plus a short-lived signed room/interview ticket in the route.
        # They have no browser session and therefore cannot provide CSRF.
        if request.path.startswith('/api/conversation/livekit/agent/'):
            return None
        streaming = request.path == '/api/voice/stream' and request.method == 'GET'
        if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'} and not streaming:
            return None
        if app.config['RATE_LIMIT_ENABLED'] and os.getenv('DATABASE_URL'):
            from app.database import db
            path = request.path
            if path in {'/login', '/signup'}:
                limit = int(os.getenv('RATE_LIMIT_AUTH_PER_MINUTE', '12'))
                category = 'auth'
            else:
                limit = int(os.getenv('RATE_LIMIT_AUDIO_PER_MINUTE', '30') if path in {'/api/transcribe', '/api/synthesize', '/api/voice/stream'}
                            else os.getenv('RATE_LIMIT_INTERVIEW_PER_MINUTE', '60'))
                category = 'audio' if path in {'/api/transcribe', '/api/synthesize', '/api/voice/stream'} else 'interview'
            if limit < 1:
                raise ValueError('Rate limit must be positive.')
            bucket = int(time.time()) // 60
            key = hashlib.sha256((request.remote_addr or 'unknown').encode()).hexdigest()
            try:
                with db.engine.begin() as connection:
                    count = connection.execute(text('''INSERT INTO request_limits (client_hash, category, bucket, count)
                        VALUES (:client, :category, :bucket, 1)
                        ON CONFLICT (client_hash, category, bucket)
                        DO UPDATE SET count = request_limits.count + 1 RETURNING count'''),
                        {'client': key, 'category': category, 'bucket': bucket}).scalar_one()
                if count > limit:
                    response = jsonify(error='Too many requests. Please try again shortly.')
                    response.status_code = 429
                    response.headers['Retry-After'] = str(60 - int(time.time()) % 60)
                    return response
            except Exception:
                return jsonify(error='Service temporarily unavailable.'), 503
        if app.config['CSRF_ENABLED'] and not streaming:
            expected = session.get('_csrf_token')
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
            if not isinstance(expected, str) or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
                return jsonify(error='Invalid or expired request token. Refresh the page and try again.'), 403
        return None
