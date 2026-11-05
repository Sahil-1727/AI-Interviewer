"""Demo-inspired agent setup checks without model or network calls."""
import unittest
from unittest.mock import patch

try:
    from livekit_agent.conversational_agent import (
        ConversationalInterviewAgent, create_conversational_session, prepare_context,
    )
except ModuleNotFoundError:
    ConversationalInterviewAgent = None


@unittest.skipIf(ConversationalInterviewAgent is None, 'requires optional LiveKit agent dependencies')
class ConversationalAgentTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            'candidate_name': 'Candidate',
            'evaluation_context': {'job_description': 'Junior Python role', 'resume': 'Built a project'},
            'interview_plan': {'questions': [
                {'question': 'Explain lists.', 'skill': 'Python'},
                {'question': 'Describe a class.', 'skill': 'Python'},
            ]},
        }

    def test_agent_uses_approved_plan_and_treats_context_as_data(self):
        agent = ConversationalInterviewAgent(self.context)
        self.assertIn('Explain lists.', agent.instructions)
        self.assertIn('Junior Python role', agent.instructions)
        self.assertIn('untrusted reference data', agent.instructions)

    def test_missing_or_malformed_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_context({'interview_plan': {'questions': []}})
        with self.assertRaises(ValueError):
            prepare_context({'interview_plan': {'questions': [None]}})

    def test_reconnect_context_preserves_progress_and_rejects_bad_index(self):
        context = {**self.context, 'current_question': 1,
                   'interview_history': [{'question': 'Explain lists.', 'answer': 'An ordered collection.'}],
                   'current_follow_up': {'question': 'Can you give an example?'}}
        result = prepare_context(context)
        self.assertEqual(result['current_question'], 1)
        self.assertEqual(result['current_follow_up'], 'Can you give an example?')
        self.assertEqual(result['completed_answers'][0]['answer'], 'An ordered collection.')
        with self.assertRaises(ValueError):
            prepare_context({**self.context, 'current_question': 2})

    def test_session_config_matches_demo_and_deduplicates_keyterms(self):
        with patch('livekit_agent.conversational_agent.inference.STT') as stt, \
             patch('livekit_agent.conversational_agent.inference.LLM') as llm, \
             patch('livekit_agent.conversational_agent.inference.TTS') as tts, \
             patch('livekit_agent.conversational_agent.inference.TurnDetector'), \
             patch('livekit_agent.conversational_agent.AgentSession') as session:
            create_conversational_session(self.context)
        stt.assert_called_once_with(model='assemblyai/universal-3-6-pro', language='en')
        llm.assert_called_once_with(model='google/gemma-4-31b-it')
        tts.assert_called_once_with(model='fishaudio/s2.1-pro', voice='fa4c9eb3dccc4806b382b40d61c6b10a')
        self.assertEqual(session.call_args.kwargs['stt_context_options']['keyterms'], ['Python'])
        self.assertTrue(session.call_args.kwargs['expressive'])
