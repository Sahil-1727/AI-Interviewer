
import os
import tempfile
import re
from threading import Lock

from faster_whisper import WhisperModel
from app.voice_config import voice_config

_model = None
_model_lock = Lock()
_transcribe_lock = Lock()


def get_whisper_model():
    global _model

    if _model is None:
        with _model_lock:
            if _model is None:
                _model = WhisperModel(
                    os.getenv("WHISPER_MODEL", "base"),
                    device="cpu",
                    compute_type="int8"
                )

    return _model


def completion_result(segments, settings):
    """Only an isolated final Whisper segment may be a completion command.

    Decoder scores are conservative gates, not calibrated probabilities of user
    intent. Quoted commands and confident hallucinations can still fool STT.
    """
    if not segments:
        return False
    last = segments[-1]
    normalized = re.sub(r"[^a-z\s']", "", last.text.lower().replace("’", "'"))
    normalized = " ".join(normalized.split())
    commands = {"i'm done", "i am done", "i have finished my answer", "that's the end of my answer"}
    return (
        normalized in commands
        and getattr(last, "avg_logprob", float("-inf")) >= settings["COMPLETION_MIN_LOGPROB"]
        and getattr(last, "no_speech_prob", 1) <= settings["COMPLETION_MAX_NO_SPEECH_PROB"]
    )


def transcribe_audio(audio_file, detect_completion=False):
    if not audio_file or not audio_file.filename:
        raise ValueError("Audio file is required.")

    allowed_extensions = {
    ".wav", ".mp3", ".m4a", ".mp4", ".webm", ".ogg"}
    extension = os.path.splitext(audio_file.filename.lower())[1]

    if extension not in allowed_extensions:
        raise ValueError("Unsupported audio format.")

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=extension
        ) as temp_file:
            audio_file.save(temp_file)
            temp_path = temp_file.name

        model = get_whisper_model()

        with _transcribe_lock:
            segments, info = model.transcribe(
                temp_path,
                language="en",
                vad_filter=True,
                beam_size=1
            )

            segments = list(segments)
            transcript = " ".join(
                segment.text.strip()
                for segment in segments
            ).strip()

        if not transcript:
            raise ValueError("No speech detected.")

        result = {
            "transcript": transcript,
            "language": info.language
        }
        if detect_completion:
            result["completion_detected"] = completion_result(segments, voice_config())
            if result["completion_detected"]:
                result["transcript"] = " ".join(segment.text.strip() for segment in segments[:-1]).strip()
        return result

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
