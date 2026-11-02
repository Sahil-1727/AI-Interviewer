"""Experimental browser turn-taking settings (seconds unless named otherwise)."""
import math
import os


DEFAULTS = {
    "INITIAL_THINKING_PROMPT_SECONDS": 15,
    "END_OF_ANSWER_SILENCE_SECONDS": 5,
    "COMMAND_CHECKPOINT_SECONDS": 1,
    "AUDIO_SAMPLE_INTERVAL_MS": 50,
    "AUDIO_LEVEL_THRESHOLD": 0.025,
    "MIN_ACTIVITY_SECONDS": 0.25,
    "PLAYBACK_SETTLE_SECONDS": 0.6,
    "MAX_CAPTURE_SECONDS": 600,
    "REQUEST_TIMEOUT_SECONDS": 90,
    "COMPLETION_MIN_LOGPROB": -0.25,
    "COMPLETION_MAX_NO_SPEECH_PROB": 0.1,
    "SEGMENT_SECONDS": 25,
    "MAX_SEGMENT_SECONDS": 90,
    "MAX_PENDING_SEGMENTS": 8,
    "MAX_ANSWER_CHARACTERS": 60000,
    "POLL_INTERVAL_MS": 600,
    "GEMINI_TIMEOUT_SECONDS": 60,
    "TTS_TIMEOUT_SECONDS": 60,
    "CONVERSATION_WORKERS": 2,
    "CONVERSATION_QUEUE_LIMIT": 4,
    "CONVERSATION_STALE_SECONDS": 240,
    "CONVERSATION_RETENTION_SECONDS": 86400,
    "NOISE_FLOOR_MULTIPLIER": 2.5,
    "NOISE_FLOOR_SMOOTHING": 0.05,
    "MIN_VOICED_SECONDS": 0.2,
    "STT_MIN_LOGPROB": -1,
    "STT_MAX_NO_SPEECH_PROB": 0.6,
}


def voice_config():
    settings = {}
    for name, default in DEFAULTS.items():
        value = float(os.getenv(name, default))
        if not math.isfinite(value) or (value <= 0 and name not in {"COMPLETION_MIN_LOGPROB", "STT_MIN_LOGPROB"}):
            raise ValueError(f"Invalid voice setting: {name}")
        settings[name] = value
    if settings["COMMAND_CHECKPOINT_SECONDS"] >= settings["END_OF_ANSWER_SILENCE_SECONDS"]:
        raise ValueError("COMMAND_CHECKPOINT_SECONDS must be below END_OF_ANSWER_SILENCE_SECONDS")
    if settings["AUDIO_LEVEL_THRESHOLD"] >= 1 or settings["COMPLETION_MAX_NO_SPEECH_PROB"] > 1:
        raise ValueError("Audio level and probability settings must be within 0–1")
    if settings["SEGMENT_SECONDS"] >= settings["MAX_SEGMENT_SECONDS"]:
        raise ValueError("SEGMENT_SECONDS must be below MAX_SEGMENT_SECONDS")
    if settings["CONVERSATION_STALE_SECONDS"] <= settings["GEMINI_TIMEOUT_SECONDS"] * 2:
        raise ValueError("CONVERSATION_STALE_SECONDS must exceed two Gemini timeouts")
    for name in ("CONVERSATION_WORKERS", "CONVERSATION_QUEUE_LIMIT", "MAX_PENDING_SEGMENTS"):
        if settings[name] != int(settings[name]) or settings[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not 0 < settings["NOISE_FLOOR_SMOOTHING"] <= 1 or settings["STT_MAX_NO_SPEECH_PROB"] > 1:
        raise ValueError("Noise smoothing and no-speech probability must be within 0–1")
    return settings
