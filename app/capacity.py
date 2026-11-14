"""Bound synchronous provider work; background jobs have a separate bounded queue."""
import os
from threading import BoundedSemaphore

from flask import g, jsonify, request

# Leave control, answer acceptance, health and scorecard polling responsive.
EXPENSIVE_PATHS = frozenset({
    '/', '/submit-answer', '/submit-follow-up', '/api/transcribe',
    '/api/synthesize', '/api/conversation/livekit/session',
})


def init_capacity(app):
    maximum = int(os.getenv('HTTP_PROVIDER_CONCURRENCY', '2'))
    if maximum < 1:
        raise ValueError('HTTP_PROVIDER_CONCURRENCY must be positive.')
    slots = BoundedSemaphore(maximum)
    app.extensions['http_provider_slots'] = slots

    @app.before_request
    def admit_provider_request():
        if request.method != 'POST' or request.path not in EXPENSIVE_PATHS:
            return None
        # Registered after authentication and CSRF: invalid browser requests must
        # not consume the deployment's provider allowance.
        if app.config.get('RATE_LIMIT_ENABLED') and os.getenv('DATABASE_URL'):
            from app.database import db
            from app.services.request_limits import consume
            try:
                allowed, retry = consume(db.engine, 'deployment', 'provider',
                    int(os.getenv('RATE_LIMIT_PROVIDER_GLOBAL_PER_MINUTE', '30')))
            except Exception:
                return jsonify(error='Service temporarily unavailable.'), 503
            if not allowed:
                response = jsonify(error='Too many requests. Please try again shortly.')
                response.status_code = 429
                response.headers['Retry-After'] = str(retry)
                response.headers['Cache-Control'] = 'no-store'
                return response
        if not slots.acquire(blocking=False):
            response = jsonify(error='Interview service is busy. Please retry shortly.')
            response.status_code = 503
            response.headers['Retry-After'] = '2'
            response.headers['Cache-Control'] = 'no-store'
            return response
        g._provider_slot = True

    @app.teardown_request
    def release_provider_request(error):
        if g.pop('_provider_slot', False):
            slots.release()
