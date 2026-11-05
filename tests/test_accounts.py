"""Account authentication, quota, and private admin tests use SQLite only."""
import re
import unittest
from datetime import datetime, timezone

from flask import Flask
from sqlalchemy import select
from werkzeug.security import check_password_hash

from app.database import db
from app.models.interview import DailyInterviewUsage, User
from app.routes.accounts import accounts_bp, current_account, protect_accounts
from app.services.interview_repository import InterviewQuotaExceeded, create_interview
from app.security import init_security


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../app/templates', static_folder='../app/static')
        self.app.secret_key = 'account-tests-only-secret'
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(accounts_bp)
        @self.app.get('/')
        def home():
            return 'setup for ' + current_account().id
        init_security(self.app)
        protect_accounts(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.addCleanup(self.cleanup)
        self.client = self.app.test_client()

    def cleanup(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def csrf(self, path='/signup'):
        response = self.client.get(path)
        match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def signup(self, email='candidate@example.com'):
        token = self.csrf()
        return self.client.post('/signup', data={'csrf_token': token, 'display_name': 'Candidate',
            'email': email, 'password': 'CandidatePass123',
            'password_confirmation': 'CandidatePass123'})

    def test_signup_hashes_password_and_login_requires_valid_credentials(self):
        response = self.signup()
        self.assertEqual(response.status_code, 302)
        account = db.session.scalar(select(User).where(User.email == 'candidate@example.com'))
        self.assertIsNotNone(account)
        self.assertNotEqual(account.password_hash, 'CandidatePass123')
        self.assertTrue(check_password_hash(account.password_hash, 'CandidatePass123'))
        self.assertEqual(account.daily_interview_limit, 2)
        self.assertFalse(account.is_admin)
        logout_token = self.csrf('/login')
        self.client.post('/logout', data={'csrf_token': logout_token})
        token = self.csrf('/login')
        failed = self.client.post('/login', data={'csrf_token': token, 'email': account.email, 'password': 'wrong'})
        self.assertEqual(failed.status_code, 401)
        token = self.csrf('/login')
        success = self.client.post('/login', data={'csrf_token': token, 'email': account.email,
                                  'password': 'CandidatePass123'})
        self.assertEqual(success.status_code, 302)
        self.assertIn(account.id, self.client.get('/').text)

    def test_account_routes_require_login_and_admin_area_is_role_protected(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.location)
        self.assertEqual(self.client.get('/api/conversation').status_code, 401)
        self.signup()
        self.assertEqual(self.client.get('/internal/ops/accounts').status_code, 404)

    def test_super_admin_can_change_individual_limit_and_disable_account(self):
        admin = User(email='admin@example.com', display_name='Admin', is_admin=True,
                     password_hash='already-hashed')
        member = User(email='member@example.com', display_name='Member', password_hash='already-hashed')
        db.session.add_all([admin, member])
        db.session.commit()
        with self.client.session_transaction() as state:
            state['account_id'] = admin.id
        page = self.client.get('/internal/ops/accounts')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers['X-Robots-Tag'], 'noindex, nofollow, noarchive')
        self.assertNotIn(b'/internal/ops/accounts', self.client.get('/').data)
        token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        response = self.client.post('/internal/ops/accounts', data={'csrf_token': token,
            'user_id': member.id, 'daily_interview_limit': '5'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(User, member.id).daily_interview_limit, 5)
        page = self.client.get('/internal/ops/accounts')
        token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        self.client.post('/internal/ops/accounts', data={'csrf_token': token,
            'user_id': member.id, 'daily_interview_limit': '5'})
        # An unchecked box intentionally revokes login immediately.
        self.assertFalse(db.session.get(User, member.id).is_active)

    def test_daily_interview_quota_is_reserved_transactionally(self):
        member = User(email='quota@example.com', display_name='Quota user', password_hash='hash', daily_interview_limit=2)
        db.session.add(member)
        db.session.commit()
        member_id = member.id
        plan = {'questions': [{'type': 'technical', 'skill': 'Python', 'question': 'Explain lists.'}]}
        for _ in range(2):
            create_interview(db.session, member.display_name, 'Python', '', plan, owner_id=member_id)
            db.session.commit()
        usage = db.session.get(DailyInterviewUsage, (member_id, datetime.now(timezone.utc).date()))
        self.assertEqual(usage.interview_count, 2)
        with self.assertRaises(InterviewQuotaExceeded):
            create_interview(db.session, member.display_name, 'Python', '', plan, owner_id=member_id)
        db.session.rollback()


if __name__ == '__main__':
    unittest.main()
