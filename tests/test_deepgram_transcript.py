"""Streaming transcript accumulation with simulated Deepgram events."""
import unittest
from unittest.mock import patch

from app.services.deepgram_transcript import DeepgramTranscript
from app.services.stt import transcribe_audio


def result(text, start=0, final=True, **extra):
    return {'type': 'Results', 'start': start, 'is_final': final,
            'channel': {'alternatives': [{'transcript': text}]}, **extra}


class DeepgramTranscriptTests(unittest.TestCase):
    def test_vad_and_pause_events_are_not_an_answer(self):
        transcript = DeepgramTranscript()
        for event in ({'type': 'SpeechStarted'}, {'type': 'UtteranceEnd'}, result('', speech_final=True)):
            self.assertFalse(transcript.accept(event))
        self.assertEqual(transcript.transcript, '')

    def test_interim_is_never_committed_and_final_segments_are_ordered(self):
        transcript = DeepgramTranscript()
        self.assertTrue(transcript.accept(result('draft', final=False)))
        self.assertEqual(transcript.transcript, '')
        self.assertTrue(transcript.accept(result('second.', start=1.5, speech_final=True)))
        self.assertTrue(transcript.accept(result('First', start=0)))
        self.assertTrue(transcript.accept(result('second.', start=1.5, speech_final=True)))
        self.assertEqual(transcript.transcript, 'First second.')
        self.assertEqual(transcript.interim, '')

    def test_invalid_or_unidentified_final_segments_are_not_silently_appended(self):
        transcript = DeepgramTranscript()
        for start in (None, -1, float('nan'), True):
            with self.subTest(start=start), self.assertRaises(ValueError):
                transcript.accept(result('words', start=start))
        self.assertEqual(transcript.transcript, '')

    def test_conflicting_duplicate_finals_are_rejected(self):
        transcript = DeepgramTranscript()
        transcript.accept(result('original'))
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            transcript.accept(result('replacement'))
        self.assertEqual(transcript.transcript, 'original')

    def test_selecting_unimplemented_provider_does_not_silently_use_whisper(self):
        with patch.dict('os.environ', {'STT_PROVIDER': 'deepgram'}), \
             patch('app.services.stt.get_whisper_model') as whisper:
            with self.assertRaisesRegex(RuntimeError, 'not available'):
                transcribe_audio(None)
            whisper.assert_not_called()


if __name__ == '__main__':
    unittest.main()
