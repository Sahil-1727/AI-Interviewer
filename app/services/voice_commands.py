"""Recognize standalone controls and a separate final completion sentence."""
import re

COMMANDS = {
    "complete": {"i'm done", "i am done", "that's all", "that is all", "i have finished my answer", "that's the end of my answer"},
    "think": {"give me a moment", "let me think", "i need more time", "give me a moment please"},
    "repeat": {"can you repeat the question", "please repeat the question", "repeat the question", "could you repeat the question"},
}

# A final separate sentence is accepted, while quoted phrases and phrases
# embedded in a sentence remain part of the candidate's answer.
COMPLETION_SUFFIX = re.compile(r"(?i)(?<=[.!?])\s+(?:i['’]m done|i am done)[.!?\s]*$")


def normalize(text):
    return ' '.join(re.sub(r"[^a-z\s']", '', text.lower().replace('’', "'")).split())


def command_for(segments, settings):
    if not segments:
        return None
    last = segments[-1]
    confident = (getattr(last, 'avg_logprob', float('-inf')) >= settings['COMPLETION_MIN_LOGPROB']
                 and getattr(last, 'no_speech_prob', 1) <= settings['COMPLETION_MAX_NO_SPEECH_PROB'])
    if not confident:
        return None
    whole = normalize(' '.join(segment.text for segment in segments))
    text = ' '.join(segment.text.strip() for segment in segments)
    if COMPLETION_SUFFIX.search(text):
        return 'complete'
    for command, phrases in COMMANDS.items():
        # A command must occupy the WHOLE uploaded utterance. Never remove an
        # embedded or suffix phrase from an ordinary technical answer. Earlier
        # answer segments remain intact in the browser's accumulated transcript.
        if whole in phrases:
            return command
    return None


def without_completion(text):
    return COMPLETION_SUFFIX.sub('', text).strip() if COMPLETION_SUFFIX.search(text) else ''
