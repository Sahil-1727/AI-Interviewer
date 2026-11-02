"""Experimental browser turn-taking settings (seconds unless named otherwise)."""
import math
import os


DEFAULTS = {
    "INITIAL_THINKING_PROMPT_SECONDS": 15,
    "MID_ANSWER_PAUSE_SECONDS": 5,
    "LONG_PAUSE_CONFIRMATION_SECONDS": 10,
    "TURN_CONFIRMATION_GRACE_SECONDS": 10,
    "AUDIO_SAMPLE_INTERVAL_MS": 50,
    "AUDIO_LEVEL_THRESHOLD": 0.025,
    "MIN_ACTIVITY_SECONDS": 0.25,
    "PLAYBACK_SETTLE_SECONDS": 0.6,
    "MAX_CAPTURE_SECONDS": 600,
    "REQUEST_TIMEOUT_SECONDS": 90,
    "COMPLETION_MIN_LOGPROB": -0.25,
    "COMPLETION_MAX_NO_SPEECH_PROB": 0.1,
}


def voice_config():
    settings = {}
    for name, default in DEFAULTS.items():
        value = float(os.getenv(name, default))
        if not math.isfinite(value) or (value <= 0 and name != "COMPLETION_MIN_LOGPROB"):
            raise ValueError(f"Invalid voice setting: {name}")
        settings[name] = value
    if not settings["MID_ANSWER_PAUSE_SECONDS"] < settings["LONG_PAUSE_CONFIRMATION_SECONDS"]:
        raise ValueError("MID_ANSWER_PAUSE_SECONDS must precede LONG_PAUSE_CONFIRMATION_SECONDS")
    if settings["AUDIO_LEVEL_THRESHOLD"] >= 1 or settings["COMPLETION_MAX_NO_SPEECH_PROB"] > 1:
        raise ValueError("Audio level and probability settings must be within 0–1")
    return settings
