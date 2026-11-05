"""Run only against a dedicated migrated local PostgreSQL test database."""
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

import jwt


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires dedicated migrated PostgreSQL database')
class LiveKitPostgresTests(unittest.TestCase):
    def test_reconnect_and_concurrent_submissions_persist_once(self):
        import run
        from app.models.interview import Answer, Evaluation, Question
        from app.services.conversation import mock_evaluate, mock_scorecard
        from sqlalchemy import select

        env = {'LIVEKIT_ENABLED': '1', 'LIVEKIT_URL': 'wss://example.test', 'LIVEKIT_API_KEY': 'test-key',
               'LIVEKIT_API_SECRET': 'test-secret-test-secret-test-secret',
               'LIVEKIT_AGENT_SHARED_SECRET': 'worker-secret-worker-secret-worker-secret'}
        headers = {'Authorization': 'Bearer ' + env['LIVEKIT_AGENT_SHARED_SECRET']}
        client = run.app.test_client()
        with client.session_transaction() as session:
            session.clear()
            session.update(candidate_name='Test candidate', evaluation_context={'job_description': 'Java', 'resume': ''},
                interview_plan={'questions': [{'question': 'Explain Java', 'skill': 'Java', 'type': 'technical'}]},
                current_question=0, interview_history=[])
        with patch.dict(os.environ, env), patch.object(run, 'evaluate_answer', side_effect=mock_evaluate), \
             patch.object(run, 'generate_final_scorecard', side_effect=mock_scorecard):
            self.assertEqual(client.post('/api/conversation/start', json={}).status_code, 200)
            def ticket():
                response = client.post('/api/conversation/livekit/session', json={})
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                claims = jwt.decode(response.json['token'], env['LIVEKIT_API_SECRET'], algorithms=['HS256'], options={'verify_aud': False})
                return json.loads(claims['roomConfig']['agents'][0]['metadata'])['ticket']
            old_ticket, new_ticket = ticket(), ticket()
            state = client.post('/api/conversation/livekit/agent/state', json={'ticket': new_ticket}, headers=headers).json
            payload = {'turn_id': state['turn_id'], 'request_id': uuid4().hex, 'answer': 'Java is a language.'}
            self.assertEqual(client.post('/api/conversation/livekit/agent/answer', json={**payload, 'ticket': old_ticket}, headers=headers).status_code, 409)
            def send(_):
                with run.app.test_client() as c:
                    return c.post('/api/conversation/livekit/agent/answer', json={**payload, 'ticket': new_ticket}, headers=headers).status_code
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(send, range(2))), [202, 202])
            for _ in range(300):
                response = client.post('/api/conversation/livekit/agent/state', json={'ticket': new_ticket}, headers=headers)
                if response.json['status'] == 'completed':
                    break
                time.sleep(.01)
            self.assertEqual(response.json['status'], 'completed')
            with run.app.app_context(), run.app.extensions['conversation'].journal.sessions() as db_session:
                answers = db_session.scalars(select(Answer).join(Question, Answer.question_id == Question.id)
                                             .where(Question.interview_id == state['interview_id'],
                                                    Answer.transcript == payload['answer'])).all()
                self.assertEqual(len(answers), 1)
                self.assertEqual(len(db_session.scalars(select(Evaluation).where(Evaluation.answer_id == answers[0].id)).all()), 1)
