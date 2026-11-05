import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.genai.errors import ServerError

from app.services.gemini import evaluate_answer, generate_final_scorecard


class GeminiScorecardRetryTests(unittest.TestCase):
    def test_retries_one_transient_answer_evaluation_error(self):
        response = SimpleNamespace(text='{"score": 7, "strengths": [], "weaknesses": [], "feedback": "Good", "follow_up_needed": false, "follow_up_question": ""}')
        client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(
            side_effect=[ServerError(503, {'error': {'message': 'temporary'}}), response])))
        with patch('app.services.gemini.get_client', return_value=client), \
                patch.dict(os.environ, {'GEMINI_EVALUATION_RETRIES': '1',
                                        'GEMINI_EVALUATION_RETRY_DELAY_SECONDS': '0'}):
            result = evaluate_answer({'question': 'Explain lists.', 'type': 'technical'}, 'An ordered collection.')
        self.assertEqual(result['score'], 7)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_retries_one_transient_provider_server_error(self):
        response = SimpleNamespace(text='{"overall_score": 7}')
        client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(
            side_effect=[ServerError(503, {'error': {'message': 'temporary'}}), response])))
        with patch('app.services.gemini.get_client', return_value=client), \
                patch.dict(os.environ, {'GEMINI_SCORECARD_RETRIES': '1',
                                        'GEMINI_SCORECARD_RETRY_DELAY_SECONDS': '0'}):
            result = generate_final_scorecard('Candidate', [{'question': 'Q', 'evaluation': {'score': 7}}])
        self.assertEqual(result['overall_score'], 7)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_does_not_retry_non_server_errors(self):
        client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(
            side_effect=ValueError('invalid response'))))
        with patch('app.services.gemini.get_client', return_value=client), \
                patch.dict(os.environ, {'GEMINI_SCORECARD_RETRIES': '2',
                                        'GEMINI_SCORECARD_RETRY_DELAY_SECONDS': '0'}):
            with self.assertRaisesRegex(ValueError, 'invalid response'):
                generate_final_scorecard('Candidate', [])
        self.assertEqual(client.models.generate_content.call_count, 1)


if __name__ == '__main__':
    unittest.main()
