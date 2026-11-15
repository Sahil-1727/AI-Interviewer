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
from app.routes.history import history_bp
from app.models.interview import Interview, Scorecard, ConversationMessage, Question, Answer, Evaluation


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../app/templates', static_folder='../app/static')
        self.app.secret_key = 'account-tests-only-secret'
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(accounts_bp)
        self.app.register_blueprint(history_bp)
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

    def test_history_is_owned_and_cached_reports_need_no_ai(self):
        self.assertEqual(self.client.get('/my-interviews').status_code, 302)
        self.signup()
        owner = db.session.scalar(select(User).where(User.email == 'candidate@example.com'))
        other = User(display_name='Other', email='other@example.com', password_hash='hash')
        db.session.add(other)
        db.session.flush()
        first = Interview(owner_id=owner.id, job_description='Private JD', plan={'questions': []}, status='completed')
        private = Interview(owner_id=other.id, job_description='Other private JD', plan={'questions': []})
        db.session.add_all([first, private])
        db.session.flush()
        db.session.add(Scorecard(interview_id=first.id, result={'insufficient_evidence': True}, completion_status='completed', early_ended=True))
        db.session.add(ConversationMessage(interview_id=first.id, sequence=0, kind='question', text='Explain lists.'))
        db.session.commit()
        listing = self.client.get('/my-interviews')
        self.assertEqual(listing.status_code, 200)
        self.assertIn(first.id, listing.text)
        self.assertNotIn(private.id, listing.text)
        self.assertNotIn('Private JD', listing.text)
        self.assertEqual(listing.headers['Cache-Control'], 'no-store')
        for _ in range(2):
            report = self.client.get('/my-interviews/' + first.id + '/report')
            self.assertEqual(report.status_code, 200)
            self.assertIn('Insufficient evidence', report.text)
        export = self.client.get('/my-interviews/' + first.id + '/transcript')
        self.assertEqual(export.json['messages'][0]['text'], 'Explain lists.')
        self.assertIn('attachment', export.headers['Content-Disposition'])
        for suffix in ('report', 'transcript', 'review', 'conversation'):
            self.assertEqual(self.client.get('/my-interviews/' + private.id + '/' + suffix).status_code, 404)
            self.assertEqual(self.client.get('/my-interviews/missing/' + suffix).status_code, 404)

    def test_history_pagination_and_unfinished_report(self):
        self.signup()
        owner = db.session.scalar(select(User).where(User.email == 'candidate@example.com'))
        interviews = [Interview(owner_id=owner.id, job_description='JD', plan={'questions': []}) for _ in range(21)]
        db.session.add_all(interviews)
        db.session.commit()
        page = self.client.get('/my-interviews')
        self.assertIn('Next', page.text)
        filtered = self.client.get('/my-interviews?q=JD&status=pending')
        self.assertIn('q=JD', filtered.text)
        self.assertIn('status=pending', filtered.text)
        self.assertEqual(page.text.count('Download transcript'), 20)
        self.assertEqual(self.client.get('/my-interviews?page=2').text.count('Download transcript'), 1)
        self.assertEqual(self.client.get('/my-interviews?page=0').status_code, 400)
        self.assertEqual(self.client.get('/my-interviews/' + interviews[0].id + '/report').status_code, 404)

    def test_answer_review_shows_evidence_and_escapes_candidate_text(self):
        self.signup()
        owner = db.session.scalar(select(User).where(User.email == 'candidate@example.com'))
        interview = Interview(owner_id=owner.id, job_description='JD', plan={'questions': []})
        db.session.add(interview)
        db.session.flush()
        question = Question(interview_id=interview.id, sequence=0, kind='technical', skill='Python', text='Explain lists.')
        missing = Question(interview_id=interview.id, sequence=1, kind='technical', skill='Python', text='Explain tuples.')
        db.session.add_all([question, missing])
        db.session.flush()
        answer = Answer(question_id=question.id, status='answered', transcript='<script>alert(1)</script>')
        db.session.add(answer)
        db.session.flush()
        db.session.add(Evaluation(answer_id=answer.id, score=7.5, strengths=['Core concept'], weaknesses=['Add examples'], feedback='Explain mutability.', follow_up_needed=False))
        db.session.commit()
        page = self.client.get('/my-interviews/' + interview.id + '/review')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        for text in ('7.5 / 10', 'Core concept', 'Add examples', 'Explain mutability.', 'Not answered'):
            self.assertIn(text, page.text)
        self.assertIn('&lt;script&gt;', page.text)
        self.assertNotIn('<script>alert(1)</script>', page.text)
        self.assertEqual(self.client.get('/my-interviews/' + interview.id + '/review?page=0').status_code, 400)

    def test_history_filters_are_literal_and_account_scoped(self):
        self.signup()
        owner = db.session.scalar(select(User).where(User.email == 'candidate@example.com'))
        other = User(display_name='Other', password_hash='test-only')
        db.session.add(other)
        db.session.flush()
        first = Interview(owner_id=owner.id, job_description='Finance with 100% coverage', plan={'questions': []}, status='error')
        second = Interview(owner_id=owner.id, job_description='Java role', plan={'questions': []})
        private = Interview(owner_id=other.id, job_description='Finance secret', plan={'questions': []})
        db.session.add_all([first, second, private])
        db.session.flush()
        db.session.add(Question(interview_id=first.id, sequence=0, kind='technical', skill='Python', text='Explain lists.'))
        db.session.add(Scorecard(interview_id=second.id, result={'insufficient_evidence': True}, completion_status='completed', early_ended=True))
        db.session.commit()
        for query in ('Finance', 'python', '%'):
            page = self.client.get('/my-interviews', query_string={'q': query})
            self.assertEqual(page.status_code, 200)
            self.assertIn(first.id, page.text)
            self.assertNotIn(second.id, page.text)
            self.assertNotIn(private.id, page.text)
        for status, shown, hidden in (('report', second.id, first.id), ('pending', first.id, second.id), ('attention', first.id, second.id)):
            page = self.client.get('/my-interviews', query_string={'status': status})
            self.assertIn(shown, page.text)
            self.assertNotIn(hidden, page.text)
        self.assertIn('No interviews match', self.client.get('/my-interviews?q=missing').text)
        self.assertEqual(self.client.get('/my-interviews?status=invalid').status_code, 400)
        self.assertEqual(self.client.get('/my-interviews', query_string={'q': 'a' * 161}).status_code, 400)

    def test_readable_transcript_search_export_and_pagination(self):
        self.signup()
        owner = db.session.scalar(select(User).where(User.email == 'candidate@example.com'))
        interview = Interview(owner_id=owner.id, job_description='JD', plan={'questions': []})
        db.session.add(interview)
        db.session.flush()
        for sequence in range(41):
            db.session.add(ConversationMessage(interview_id=interview.id, sequence=sequence,
                kind='question' if sequence == 0 else 'answered',
                text='<script>100% saved</script>' if sequence == 0 else f'Message {sequence}'))
        db.session.commit()
        base = '/my-interviews/' + interview.id
        page = self.client.get(base + '/conversation')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count('class="transcript-message"'), 40)
        self.assertIn('&lt;script&gt;', page.text)
        self.assertNotIn('<script>100% saved</script>', page.text)
        self.assertIn('Next', page.text)
        self.assertEqual(self.client.get(base + '/conversation?page=2').text.count('class="transcript-message"'), 1)
        searched = self.client.get(base + '/conversation', query_string={'q': '%'})
        self.assertEqual(searched.text.count('class="transcript-message"'), 1)
        self.assertIn('No messages match', self.client.get(base + '/conversation?q=missing').text)
        self.assertEqual(self.client.get(base + '/conversation?page=0').status_code, 400)
        self.assertEqual(self.client.get(base + '/conversation', query_string={'q': 'x'*161}).status_code, 400)
        exported = self.client.get(base + '/transcript?format=text')
        self.assertEqual(exported.mimetype, 'text/plain')
        self.assertEqual(exported.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(exported.headers['Cache-Control'], 'no-store')
        self.assertIn('Message 40', exported.text)
        self.assertIn('interview-transcript.txt', exported.headers['Content-Disposition'])
        self.assertEqual(len(self.client.get(base + '/transcript').json['messages']), 41)
        self.assertEqual(self.client.get(base + '/transcript?format=invalid').status_code, 400)

    def practice_records(self):
        self.signup()
        owner = db.session.scalar(select(User).where(User.email == 'candidate@example.com'))
        other = User(display_name='Other', password_hash='test-only')
        db.session.add(other)
        db.session.flush()
        interviews = [Interview(owner_id=who, job_description='JD', plan={'questions': []})
                      for who in (owner.id, owner.id, other.id)]
        db.session.add_all(interviews)
        db.session.flush()
        questions = [Question(interview_id=item.id, sequence=0, kind='technical',
                     skill='Python' if i < 2 else 'PrivateSkill', text=f'Question {i}')
                     for i, item in enumerate(interviews)]
        db.session.add_all(questions)
        db.session.flush()
        for i, question in enumerate(questions):
            answer = Answer(question_id=question.id, status='answered', transcript='My explanation')
            db.session.add(answer)
            db.session.flush()
            db.session.add(Evaluation(answer_id=answer.id, score=[8, 2, 1][i],
                strengths=['Correct core concept'], weaknesses=['Explain an example'],
                feedback='Saved feedback', follow_up_needed=False))
        for item in (interviews[0], interviews[2]):
            db.session.add(Scorecard(interview_id=item.id, result={}, completion_status='completed', early_ended=True))
        db.session.commit()
        return interviews, questions

    def test_bookmark_idempotency_ownership_and_coaching(self):
        from app.models.interview import QuestionBookmark
        self.app.config['CSRF_ENABLED'] = True
        interviews, questions = self.practice_records()
        token = self.csrf('/login')
        def path(i, q):
            return f'/my-interviews/{i}/questions/{q}/bookmark'
        own = path(interviews[0].id, questions[0].id)
        self.assertEqual(self.client.post(own, data={'action': 'save'}).status_code, 403)
        for _ in range(2):
            self.assertEqual(self.client.post(own, data={'action': 'save', 'csrf_token': token}).status_code, 303)
        self.assertEqual(len(db.session.scalars(select(QuestionBookmark)).all()), 1)
        listing = self.client.get('/my-interviews/bookmarks')
        self.assertIn('Question 0', listing.text)
        self.assertNotIn('Question 2', listing.text)
        self.assertEqual(listing.headers['Cache-Control'], 'no-store')
        for route in (path(interviews[2].id, questions[2].id), path(interviews[0].id, questions[2].id)):
            self.assertEqual(self.client.post(route, data={'action': 'save', 'csrf_token': token}).status_code, 404)
        self.assertEqual(self.client.post(own, data={'action': 'toggle', 'csrf_token': token}).status_code, 400)
        review = self.client.get(f'/my-interviews/{interviews[0].id}/review?question={questions[0].id}')
        for value in ('ANSWER COACHING', 'Explain an example', 'Correct core concept', 'Remove bookmark'):
            self.assertIn(value, review.text)
        self.assertEqual(self.client.get(f'/my-interviews/{interviews[0].id}/review?question={questions[2].id}').status_code, 404)
        for _ in range(2):
            self.assertEqual(self.client.post(own, data={'action': 'remove', 'csrf_token': token}).status_code, 303)
        self.assertEqual(len(db.session.scalars(select(QuestionBookmark)).all()), 0)
        self.assertEqual(self.client.get('/my-interviews/bookmarks?page=0').status_code, 400)

    def test_progress_uses_only_owned_evaluated_answered_reports(self):
        self.assertEqual(self.client.get('/my-interviews/progress').status_code, 302)
        interviews, questions = self.practice_records()
        extra = Question(interview_id=interviews[0].id, sequence=1, kind='technical', skill='UnansweredSkill', text='No answer')
        db.session.add(extra)
        db.session.flush()
        db.session.add(Answer(question_id=extra.id, status='unanswered'))
        db.session.commit()
        response = self.client.get('/my-interviews/progress')
        self.assertEqual(response.status_code, 200)
        self.assertIn('8.0 / 10', response.text)
        self.assertIn('Python', response.text)
        self.assertNotIn('PrivateSkill', response.text)
        self.assertNotIn('UnansweredSkill', response.text)
        self.assertNotIn('2.0 / 10', response.text)
        self.assertIn(interviews[0].id, response.text)
        self.assertNotIn(interviews[1].id, response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_practice_empty_states(self):
        self.signup()
        self.assertIn('No bookmarks', self.client.get('/my-interviews/bookmarks').text)
        page = self.client.get('/my-interviews/progress')
        self.assertIn('Not available', page.text)
        self.assertIn('No evaluated interviews yet', page.text)


if __name__ == '__main__':
    unittest.main()
