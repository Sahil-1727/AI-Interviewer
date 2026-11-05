"""A provider timeout must not turn setup into an unexplained 500."""
import io
import unittest
from unittest.mock import patch

from google.genai.errors import ServerError
import run


class InterviewPlanTimeoutTests(unittest.TestCase):
    def test_timeout_keeps_setup_fields_and_does_not_replace_existing_session(self):
        client = run.app.test_client()
        with client.session_transaction() as session:
            session['candidate_name'] = 'Existing candidate'
        with patch.object(run, 'extract_resume_text', return_value='Test resume'), \
             patch.object(run, 'generate_interview_plan', side_effect=ServerError(504, {'error': {'message': 'sensitive'}})):
            response = client.post('/', data={'candidate_name': 'New candidate', 'job_description': 'Java basics',
                                            'resume': (io.BytesIO(b'test'), 'resume.pdf')})
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'Gemini took too long', response.data)
        self.assertIn(b'New candidate', response.data)
        self.assertIn(b'Java basics', response.data)
        self.assertNotIn(b'sensitive', response.data)
        with client.session_transaction() as session:
            self.assertEqual(session['candidate_name'], 'Existing candidate')
