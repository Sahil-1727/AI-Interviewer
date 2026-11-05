"""Stage 1 contract tests: no model downloads, TTS network, or Gemini calls."""
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

import run
from app.services.stt import completion_result, transcribe_audio
from app.voice_config import DEFAULTS, voice_config


def segment(text, logprob=-0.1, no_speech=0.01):
    return SimpleNamespace(text=text, avg_logprob=logprob, no_speech_prob=no_speech)


class CompletionTests(unittest.TestCase):
    def test_only_isolated_confident_commands_qualify(self):
        for text in ("I'm done.", "I am done!", "I have finished my answer.", "That’s the end of my answer."):
            with self.subTest(text=text):
                self.assertTrue(completion_result([segment(text)], DEFAULTS))
        for text in ("Then I said I'm done.", "I'm done with the first part", "done"):
            with self.subTest(text=text):
                self.assertFalse(completion_result([segment(text)], DEFAULTS))
        self.assertFalse(completion_result([segment("I'm done", logprob=-1)], DEFAULTS))
        self.assertFalse(completion_result([segment("I'm done", no_speech=0.8)], DEFAULTS))
        self.assertFalse(completion_result([SimpleNamespace(text="I'm done")], DEFAULTS))
        self.assertFalse(completion_result([], DEFAULTS))

    def transcribe(self, segments, detect_completion=False):
        audio = FileStorage(stream=io.BytesIO(b"mock audio"), filename="answer.webm")
        model = SimpleNamespace(transcribe=lambda *a, **kw: (iter(segments), SimpleNamespace(language="en", duration_after_vad=1)))
        with patch("app.services.stt.get_whisper_model", return_value=model), patch("app.services.stt.decode_bounded", return_value=[]):
            return transcribe_audio(audio, detect_completion=detect_completion)

    def test_manual_transcript_preserves_completion_words(self):
        result = self.transcribe([segment("My example."), segment("I'm done.")])
        self.assertEqual(result, {"transcript": "My example. I'm done.", "language": "en"})

    def test_auto_command_is_removed_only_when_confident(self):
        result = self.transcribe([segment("I'm done.")], True)
        self.assertEqual(result["transcript"], "")
        self.assertTrue(result["completion_detected"])
        result = self.transcribe([segment("I built a project."), segment("I'm done.")], True)
        self.assertEqual(result["transcript"], "I built a project.")
        self.assertEqual(result["raw_transcript"], "I built a project. I'm done.")
        result = self.transcribe([segment("I'm done.", logprob=-1)], True)
        self.assertEqual(result["transcript"], "I'm done.")
        self.assertFalse(result["completion_detected"])

    def test_command_only_and_no_speech(self):
        self.assertEqual(self.transcribe([segment("I'm done.")], True)["transcript"], "")
        with self.assertRaisesRegex(ValueError, "No speech detected"):
            self.transcribe([], True)


class VoiceRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous_testing = run.app.testing
        run.app.testing = True

    def tearDown(self):
        run.app.testing = self.previous_testing

    def test_transcribe_cannot_evaluate_or_advance_session(self):
        with (
            run.app.test_client() as client,
            patch.object(run, "transcribe_audio", return_value={"transcript": "Test answer", "completion_detected": False}) as stt,
            patch.object(run, "evaluate_answer") as evaluate,
            patch.object(run, "generate_interview_plan") as plan,
            patch.object(run, "generate_final_scorecard") as scorecard,
        ):
            with client.session_transaction() as state:
                state["current_question"] = 3
                state["interview_history"] = [{"answer": "Earlier"}]
            response = client.post("/api/transcribe", data={
                "audio": (io.BytesIO(b"audio"), "answer.webm"), "detect_completion": "true"
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["transcript"], "Test answer")
            self.assertTrue(stt.call_args.kwargs["detect_completion"])
            with client.session_transaction() as state:
                self.assertEqual(state["current_question"], 3)
                self.assertEqual(state["interview_history"], [{"answer": "Earlier"}])
            evaluate.assert_not_called()
            plan.assert_not_called()
            scorecard.assert_not_called()

    def test_manual_request_keeps_original_contract(self):
        with run.app.test_client() as client, patch.object(run, "transcribe_audio", return_value={"transcript": "Manual"}) as stt:
            self.assertEqual(client.post("/api/transcribe", data={"audio": (io.BytesIO(b"audio"), "a.webm")}).status_code, 200)
            self.assertEqual(stt.call_args.kwargs, {})

    def test_voice_controls_render_on_main_and_follow_up(self):
        with run.app.test_client() as client:
            question = {"question": "Explain a project", "skill": "Python", "type": "technical"}
            with client.session_transaction() as state:
                state["interview_plan"] = {"questions": [question]}
                state["current_question"] = 0
                state["current_follow_up"] = dict(question, type="follow_up")
            for path in ("/start-interview", "/follow-up"):
                page = client.get(path)
                self.assertEqual(page.status_code, 200)
                for control in (b'automatic-voice', b'Done Speaking', b'pause-voice', b'voice-turns.js', b'candidate-video',
                                b'Before Your Interview Begins', b'instructions-acknowledged', b'check-microphone', b'check-camera',
                                b'I have read and understood the interview instructions.'):
                    self.assertIn(control, page.data)
                self.assertNotIn(b'id="automatic-voice" checked', page.data)

    def test_settings_reject_invalid_thresholds(self):
        for overrides in ({"COMMAND_CHECKPOINT_SECONDS": "11"}, {"AUDIO_LEVEL_THRESHOLD": "nan"}, {"MAX_CAPTURE_SECONDS": "-1"}):
            with patch.dict(os.environ, overrides), self.assertRaises(ValueError):
                voice_config()
