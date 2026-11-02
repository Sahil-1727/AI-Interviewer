
import os
import tempfile
from threading import Lock

from faster_whisper import WhisperModel

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


def transcribe_audio(audio_file):
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

            transcript = " ".join(
                segment.text.strip()
                for segment in segments
            ).strip()

        if not transcript:
            raise ValueError("No speech detected.")

        return {
            "transcript": transcript,
            "language": info.language
        }

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
