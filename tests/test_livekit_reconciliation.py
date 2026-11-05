import unittest

from app.services.conversation import ConversationService


class LiveKitTranscriptReconciliationTests(unittest.TestCase):
    def test_opening_small_talk_is_not_scored_as_an_answer(self):
        snapshot = {
            'interview_plan': {'questions': [
                {'question': 'Explain the difference between a list and a tuple.',
                 'skill': 'Python', 'type': 'technical'},
            ]},
            'current_question': 0,
            'evaluation_context': {},
            'livekit_events': [
                {'kind': 'interviewer', 'text': 'Hi Sahil, how are you today? Are you comfortable?'
                 , 'event_id': '1' * 32},
                {'kind': 'candidate', 'text': 'I am good, thank you.', 'event_id': '2' * 32},
                {'kind': 'interviewer', 'text': 'Explain the difference between a list and a tuple.',
                 'event_id': '3' * 32},
                {'kind': 'candidate', 'text': 'A list is mutable, while a tuple is immutable.',
                 'event_id': '4' * 32},
            ],
        }
        rows, answered, next_main, pending = ConversationService.reconcile_livekit_transcript(snapshot)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['answer'], 'A list is mutable, while a tuple is immutable.')
        self.assertEqual(answered, {0})
        self.assertEqual(next_main, 0)
        self.assertIsNone(pending)


if __name__ == '__main__':
    unittest.main()
