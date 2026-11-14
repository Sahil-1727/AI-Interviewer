"""Browser CSRF validation and PostgreSQL-backed request limits."""
import hmac
import os
import secrets

from flask import jsonify, request, session
from app.services.request_limits import consume


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
        worker = request.path.startswith('/api/conversation/livekit/agent/')
        streaming = request.path == '/api/voice/stream' and request.method == 'GET'
        polling = request.method == 'GET' and (request.path == '/api/conversation'
            or request.path == '/my-interviews' or request.path.startswith('/my-interviews/'))
        if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'} and not streaming and not polling:
            return None
        if app.config['RATE_LIMIT_ENABLED'] and os.getenv('DATABASE_URL'):
            from app.database import db
            path = request.path
            if worker:
                category, setting, default = 'worker', 'RATE_LIMIT_WORKER_PER_MINUTE', '600'
            elif path in {'/login', '/signup'}:
                category, setting, default = 'auth', 'RATE_LIMIT_AUTH_PER_MINUTE', '12'
            elif polling:
                category, setting, default = 'read', 'RATE_LIMIT_READ_PER_MINUTE', '120'
            elif path in {'/api/transcribe', '/api/synthesize', '/api/voice/stream'}:
                category, setting, default = 'audio', 'RATE_LIMIT_AUDIO_PER_MINUTE', '30'
            else:
                category, setting, default = 'interview', 'RATE_LIMIT_INTERVIEW_PER_MINUTE', '60'
            checks = [('ip:' + (request.remote_addr or 'unknown'), category, int(os.getenv(setting, default)))]
            if not worker and session.get('account_id'):
                checks.append(('account:' + str(session['account_id']), category, int(os.getenv(setting, default))))
            try:
                for identity, category, limit in checks:
                    allowed, retry = consume(db.engine, identity, category, limit)
                    if not allowed:
                        response = jsonify(error='Too many requests. Please try again shortly.')
                        response.status_code = 429
                        response.headers['Retry-After'] = str(retry)
                        response.headers['Cache-Control'] = 'no-store'
                        return response
            except Exception:
                return jsonify(error='Service temporarily unavailable.'), 503
        # Worker endpoints verify their shared secret and signed interview ticket.
        if worker:
            return None
        if app.config['CSRF_ENABLED'] and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            expected = session.get('_csrf_token')
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
            if not isinstance(expected, str) or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
                return jsonify(error='Invalid or expired request token. Refresh the page and try again.'), 403
        return None
