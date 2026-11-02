
import os
import tempfile
import time

import av
import numpy as np
from threading import Lock

from faster_whisper import WhisperModel
from app.voice_config import voice_config
from app.services.voice_commands import command_for

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
    """Only a confident, standalone uploaded utterance may be a command.

    Decoder scores are conservative gates, not calibrated probabilities of user
    intent. Quoted commands and confident hallucinations can still fool STT.
    """
    return command_for(segments, settings) == "complete"


def decode_bounded(path, limit):
    """Decode incrementally so compressed uploads cannot expand without bound."""
    samples, count = [], 0
    with av.open(path) as container:
        if not container.streams.audio:
            raise ValueError("No audio track detected.")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            for output in resampler.resample(frame):
                count += output.samples
                if count > limit * 16000:
                    raise ValueError("Audio segment exceeds the duration limit. Use shorter segments.")
                samples.append(output.to_ndarray().reshape(-1))
        for output in resampler.resample(None):
            count += output.samples
            if count > limit * 16000:
                raise ValueError("Audio segment exceeds the duration limit.")
            samples.append(output.to_ndarray().reshape(-1))
    if not count:
        raise ValueError("No speech detected.")
    return np.concatenate(samples).astype(np.float32) / 32768.0



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

        settings = voice_config()
        try:
            audio = decode_bounded(temp_path, settings["MAX_SEGMENT_SECONDS"])
        except av.error.FFmpegError as error:
            raise ValueError("Audio could not be decoded. Record again using a supported browser.") from error
        model = get_whisper_model()
        started = time.perf_counter()

        with _transcribe_lock:
            segments, info = model.transcribe(
                audio,
                language="en",
                vad_filter=True,
                beam_size=1
            )

            segments = list(segments)
            if detect_completion:
                # Silero VAD is built into faster-whisper. Energy alone cannot
                # arm evaluation: require actual VAD-positive decoded audio and
                # conservative decoder quality, too. Manual STT stays permissive.
                if getattr(info, "duration_after_vad", 0) < settings["MIN_VOICED_SECONDS"]:
                    raise ValueError("No speech detected.")
                segments = [segment for segment in segments
                            if getattr(segment, "avg_logprob", float("-inf")) >= settings["STT_MIN_LOGPROB"]
                            and getattr(segment, "no_speech_prob", 1) <= settings["STT_MAX_NO_SPEECH_PROB"]]
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
            command = command_for(segments, settings)
            result.update(raw_transcript=transcript, command=command, completion_detected=command == "complete",
                          speech_detected=True, voiced_seconds=round(getattr(info, "duration_after_vad", 0), 3),
                          whisper_ms=round((time.perf_counter() - started) * 1000, 2))
            if command:
                result["transcript"] = ""
        return result

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
