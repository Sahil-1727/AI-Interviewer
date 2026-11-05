"""Self-service accounts and an unlinked, admin-only account console."""
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import click
from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from app.database import db
from app.models.interview import DailyInterviewUsage, Interview, User


accounts_bp = Blueprint('accounts', __name__)
EMAIL_PATTERN = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


def current_account():
    return getattr(g, 'current_account', None)


def daily_usage(account_id):
    if not account_id:
        return 0, 2
    account = db.session.get(User, account_id)
    if account is None:
        return 0, 2
    today = datetime.now(timezone.utc).date()
    usage = db.session.get(DailyInterviewUsage, (account.id, today))
    return (usage.interview_count if usage else 0), account.daily_interview_limit


def protect_accounts(app):
    """Require an active account everywhere except auth, health, static and worker calls."""
    @app.before_request
    def load_authenticated_account():
        if current_app.config.get('TESTING') or current_app.config.get('AUTH_DISABLED_FOR_TESTS'):
            g.current_account = SimpleNamespace(id=session.get('account_id'),
                                                display_name=session.get('candidate_name', 'Candidate'))
            return None
        path = request.path
        public = (path.startswith('/static/') or path in {'/login', '/signup', '/health/ready', '/favicon.ico'}
                  or path.startswith('/api/conversation/livekit/agent/'))
        if public:
            return None
        account_id = session.get('account_id')
        if not account_id:
            if path.startswith('/api/'):
                return jsonify(error='Sign in to continue.'), 401
            return redirect(url_for('accounts.login', next=path))
        try:
            account = db.session.get(User, account_id)
        except SQLAlchemyError:
            db.session.rollback()
            return jsonify(error='Account service is temporarily unavailable.'), 503
        if account is None or not account.is_active or not account.password_hash:
            session.clear()
            if path.startswith('/api/'):
                return jsonify(error='Sign in to continue.'), 401
            return redirect(url_for('accounts.login'))
        g.current_account = account

    @app.context_processor
    def account_template_context():
        return {'current_account': current_account()}

    @app.cli.command('create-super-admin')
    @click.argument('email')
    @click.password_option(prompt='Password', confirmation_prompt=True)
    def create_super_admin(email, password):
        """Create the one administrator account from a trusted server shell."""
        normalized = (email or '').strip().lower()
        if len(normalized) > 320 or not EMAIL_PATTERN.fullmatch(normalized):
            raise click.ClickException('Provide a valid email address.')
        if not 8 <= len(password) <= 20:
            raise click.ClickException('Use a password between 8 and 20 characters.')
        if db.session.scalar(select(User.id).where(User.is_admin.is_(True))) is not None:
            raise click.ClickException('A super-admin already exists. Use the existing administrator account.')
        user = db.session.scalar(select(User).where(User.email == normalized))
        if user is None:
            user = User(email=normalized, display_name='Super Admin', is_admin=True,
                        is_active=True, password_hash=generate_password_hash(password))
            db.session.add(user)
        else:
            user.password_hash = generate_password_hash(password)
            user.is_admin = True
            user.is_active = True
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise click.ClickException('Could not create the administrator account.') from error
        click.echo('Super-admin account is ready.')


@accounts_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if not os_database_available():
        return render_template('signup.html', error='Account registration is temporarily unavailable.'), 503
    if request.method == 'GET':
        return render_template('signup.html')
    name = request.form.get('display_name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    confirmation = request.form.get('password_confirmation', '')
    if not name or len(name) > 255:
        return render_template('signup.html', error='Enter a name up to 255 characters.', display_name=name, email=email), 400
    if len(email) > 320 or not EMAIL_PATTERN.fullmatch(email):
        return render_template('signup.html', error='Enter a valid email address.', display_name=name, email=email), 400
    if not 8 <= len(password) <= 20:
        return render_template('signup.html', error='Choose a password between 8 and 20 characters.', display_name=name, email=email), 400
    if password != confirmation:
        return render_template('signup.html', error='The passwords do not match.', display_name=name, email=email), 400
    user = User(email=email, display_name=name, password_hash=generate_password_hash(password),
                daily_interview_limit=2, is_admin=False, is_active=True)
    try:
        db.session.add(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return render_template('signup.html', error='Could not create an account with those details.', display_name=name, email=email), 409
    except SQLAlchemyError as error:
        db.session.rollback()
        sqlstate = getattr(getattr(error, 'orig', None), 'sqlstate', None)
        current_app.logger.warning('Account signup database failure: error_type=%s sqlstate=%s',
                                   type(error).__name__, sqlstate or 'unknown')
        return render_template('signup.html',
            error='Account storage is unavailable or needs a schema update. Please try again later.',
            display_name=name, email=email), 503
    session.clear()
    session['account_id'] = user.id
    session['candidate_name'] = user.display_name
    return redirect(url_for('home'))


@accounts_bp.route('/login', methods=['GET', 'POST'])
def login():
    if not os_database_available():
        return render_template('login.html', error='Sign-in is temporarily unavailable.'), 503
    if request.method == 'GET':
        return render_template('login.html', next_path=safe_next(request.args.get('next')))
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    user = db.session.scalar(select(User).where(User.email == email)) if len(email) <= 320 else None
    valid = bool(user and user.password_hash and user.is_active and len(password) <= 256
                 and check_password_hash(user.password_hash, password))
    if not valid:
        return render_template('login.html', error='Invalid email or password.', email=email,
                               next_path=safe_next(request.form.get('next'))), 401
    session.clear()
    session['account_id'] = user.id
    session['candidate_name'] = user.display_name
    return redirect(safe_next(request.form.get('next')) or url_for('home'))


@accounts_bp.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('accounts.login'))


@accounts_bp.route('/internal/ops/accounts', methods=['GET', 'POST'])
def account_console():
    account = current_account()
    if account is None or not account.is_admin:
        return 'Not found.', 404
    if request.method == 'POST':
        user_id = request.form.get('user_id', '')
        target = db.session.get(User, user_id)
        if target is None or target.is_admin:
            return 'Account not found.', 404
        try:
            limit = int(request.form.get('daily_interview_limit', ''))
        except (TypeError, ValueError):
            limit = -1
        if not 0 <= limit <= 100:
            flash('Daily interview limit must be between 0 and 100.', 'error')
            return redirect(url_for('accounts.account_console'))
        target.daily_interview_limit = limit
        target.is_active = request.form.get('is_active') == '1'
        db.session.commit()
        flash('Account limits updated.', 'success')
        return redirect(url_for('accounts.account_console'))

    accounts = db.session.scalars(select(User).where(User.is_admin.is_(False)).order_by(User.created_at.desc())).all()
    today = datetime.now(timezone.utc).date()
    usage = {row.user_id: row.interview_count for row in db.session.scalars(
        select(DailyInterviewUsage).where(DailyInterviewUsage.usage_date == today)).all()}
    total = {user_id: count for user_id, count in db.session.execute(
        select(Interview.owner_id, func.count(Interview.id)).group_by(Interview.owner_id)).all()}
    response = current_app.make_response(render_template('account_console.html', accounts=accounts,
        daily_usage=usage, total_interviews=total, today=today.isoformat()))
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response.headers['Cache-Control'] = 'no-store'
    return response


def os_database_available():
    return bool(current_app.config.get('SQLALCHEMY_DATABASE_URI'))


def safe_next(value):
    if isinstance(value, str) and value.startswith('/') and not value.startswith('//') and '\\' not in value:
        return value
    return None
