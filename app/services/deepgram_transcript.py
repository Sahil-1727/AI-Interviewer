"""Accumulate finalized Nova streaming segments without making turn decisions.

This module deliberately does not implement a timer or start/stop audio. The
conversation controller remains responsible for deciding when a turn ends.
"""
import math


class DeepgramTranscript:
    def __init__(self):
        self._segments = {}
        self.interim = ''

    def accept(self, event):
        """Return whether the event contains recognized words.

        Only stable, final results enter the answer. Pause and VAD events have
        no bearing on whether the candidate provided a valid answer.
        """
        if not isinstance(event, dict) or event.get('type') != 'Results':
            return False
        channel = event.get('channel')
        alternatives = channel.get('alternatives') if isinstance(channel, dict) else None
        alternative = alternatives[0] if isinstance(alternatives, list) and alternatives else None
        text = alternative.get('transcript') if isinstance(alternative, dict) else None
        if not isinstance(text, str):
            return False
        text = text.strip()
        if event.get('is_final') is not True:
            self.interim = text
            return bool(text)
        self.interim = ''
        if not text:
            return False
        start = event.get('start')
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
            raise ValueError('A final transcript segment must have a valid start time.')
        # Deepgram identifies a segment by its position in this connection.
        # Conflicting finals must not silently replace already committed words.
        previous = self._segments.get(float(start))
        if previous is not None and previous != text:
            raise ValueError('Conflicting final transcript segment.')
        self._segments[float(start)] = text
        return True

    @property
    def transcript(self):
        return ' '.join(self._segments[start] for start in sorted(self._segments))
