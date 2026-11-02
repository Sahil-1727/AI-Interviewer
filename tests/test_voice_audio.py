import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np
from werkzeug.datastructures import FileStorage
from faster_whisper.vad import get_speech_timestamps
from app.services.stt import decode_bounded, transcribe_audio
from app.services.voice_commands import command_for
from app.voice_config import DEFAULTS


class VoiceAudioTests(unittest.TestCase):
    def wav(self, seconds=1):
        data = io.BytesIO()
        with wave.open(data, 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(bytes(int(seconds * 16000) * 2))
        return data.getvalue()

    def segment(self, text):
        return SimpleNamespace(text=text, avg_logprob=-.1, no_speech_prob=.01)

    def test_standalone_commands_and_separate_completion_sentence(self):
        for phrase, command in [("That's all.", 'complete'), ('Give me a moment.', 'think'),
                                ('Let me think.', 'think'), ('Can you repeat the question?', 'repeat')]:
            self.assertEqual(command_for([self.segment(phrase)], DEFAULTS), command)
        self.assertEqual(command_for([self.segment("My example."), self.segment("I'm done.")], DEFAULTS), 'complete')
        for parts in (['I would say let me think before debugging.'],
                      ['The log message says that is all.'], ['Can you repeat the question in an interview is a reasonable request.']):
            self.assertIsNone(command_for([self.segment(text) for text in parts], DEFAULTS))

    def test_real_audio_decoder_bounds_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.wav'
            path.write_bytes(self.wav())
            samples = decode_bounded(str(path), 2)
            self.assertEqual(samples.dtype, np.float32)
            self.assertEqual(len(samples), 16000)
            with self.assertRaisesRegex(ValueError, 'duration limit'):
                decode_bounded(str(path), .5)

    def test_bundled_real_silero_vad_rejects_silence_without_model_download(self):
        self.assertEqual(get_speech_timestamps(np.zeros(16000, dtype=np.float32)), [])

    def test_temp_file_cleanup_and_zero_voiced_audio_never_returns_an_answer(self):
        info = SimpleNamespace(language='en', duration_after_vad=0)
        model = SimpleNamespace(transcribe=lambda *a, **kw: (iter([self.segment('A hallucination.')]), info))
        created = []
        original = tempfile.NamedTemporaryFile
        def temporary(*a, **kw):
            file = original(*a, **kw); created.append(file.name); return file
        with patch('app.services.stt.get_whisper_model', return_value=model), patch('app.services.stt.tempfile.NamedTemporaryFile', side_effect=temporary):
            with self.assertRaisesRegex(ValueError, 'No speech detected'):
                transcribe_audio(FileStorage(stream=io.BytesIO(self.wav()), filename='a.wav'), True)
        self.assertTrue(created)
        self.assertTrue(all(not Path(name).exists() for name in created))


if __name__ == '__main__':
    unittest.main()
