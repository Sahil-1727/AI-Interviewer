"""Administrator-provisioned account access for the deployment branch."""
import json
import os
import hashlib
import hmac

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint('access', __name__)
_DUMMY_HASH = generate_password_hash('not-a-real-password')


def enabled():
    return os.getenv('APP_ENV') == 'production' or os.getenv('RESTRICTED_ACCESS') == '1'


def accounts():
    try:
        data = json.loads(os.getenv('RESTRICTED_ACCOUNTS_JSON', '{}'))
    except (ValueError, TypeError):
        raise RuntimeError('RESTRICTED_ACCOUNTS_JSON must contain a JSON object.') from None
    if not isinstance(data, dict) or not data or any(
        not isinstance(name, str) or not name.strip() or len(name) > 128 or
        not isinstance(value, str) or not value.startswith(('scrypt:', 'pbkdf2:'))
        for name, value in data.items()
    ):
        raise RuntimeError('RESTRICTED_ACCOUNTS_JSON must contain account names and password hashes.')
    return data


def account_version(password_hash):
    return hashlib.sha256(password_hash.encode('utf-8')).hexdigest()


def init_auth(app):
    if not enabled():
        return
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('Restricted access requires PostgreSQL-backed sessions.')
    accounts()  # Fail closed when production credentials are absent or malformed.
    app.register_blueprint(bp)

    @app.before_request
    def require_login():
        if (request.endpoint in {'access.login', 'access.logout', 'static', 'readiness'} or
                request.path in {'/api/conversation/livekit/agent/state', '/api/conversation/livekit/agent/answer'}):
            return None
        configured = accounts()
        name = session.get('account_name')
        version = session.get('account_version', '')
        if name not in configured or not isinstance(version, str) or not hmac.compare_digest(version, account_version(configured[name])):
            if request.path.startswith('/api/'):
                return {'error': 'Sign in to continue.'}, 401
            return redirect(url_for('access.login'))
        return None


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        configured = accounts()
        value = configured.get(name, _DUMMY_HASH)
        if len(password) <= 1024 and check_password_hash(value, password) and name in configured:
            session.clear()
            session['account_name'] = name
            session['account_version'] = account_version(value)
            current_app.session_interface.regenerate(session)
            # SharedSessionInterface otherwise treats the new ID as an
            # existing deleted session and refuses to create its record.
            if hasattr(session, '_existing_sid'):
                session._existing_sid = False
                session._initial_data = {}
            return redirect(url_for('home'))
        return render_template('login.html', login_error='Invalid username or password.'), 401
    return render_template('login.html')


@bp.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('access.login'))
