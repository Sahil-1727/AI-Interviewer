"""Ordered, idempotent transcript delivery for the LiveKit conversational agent.

Events are durably written to Flask/PostgreSQL as they arrive. The bounded
in-process queue retries transient failures but cannot recover unflushed events
after a worker process crash.
"""
import asyncio
import hashlib
import logging
from uuid import uuid4

import httpx
from livekit.agents.llm import ChatMessage

log = logging.getLogger('interview_agent')


class TranscriptWriter:
    def __init__(self, backend, on_failure, *, attempts=3, retry_delay=0.2):
        self.backend = backend
        self.on_failure = on_failure
        self.attempts = attempts
        self.retry_delay = retry_delay
        self.queue = asyncio.Queue(maxsize=100)
        self.turn_id = uuid4().hex
        self.worker = None
        self.failed = False
        self.seen = set()

    def start(self, session):
        self.worker = asyncio.create_task(self._run())

        @session.on('conversation_item_added')
        def on_item(event):
            item = event.item
            if not isinstance(item, ChatMessage) or item.role not in {'user', 'assistant'}:
                return
            text = item.text_content
            if not isinstance(text, str) or not text.strip():
                return
            if item.id in self.seen:
                return
            if item.role == 'assistant':
                self.turn_id = uuid4().hex
            metrics = item.metrics or {}
            stages = ({'stt_finalization': 'transcription_delay',
                       'end_of_speech_detection': 'end_of_turn_delay'} if item.role == 'user'
                      else {'gemini_first_token': 'llm_node_ttft',
                            'tts_first_byte': 'tts_node_ttfb',
                            'gemini_to_tts_first_sentence': 'llm_node_ttfs'})
            for stage, key in stages.items():
                value = metrics.get(key)
                if isinstance(value, (int, float)):
                    log.info('voice_metric stage=%s ms=%.1f', stage, value * 1000)
            event_id = hashlib.sha256(item.id.encode('utf-8')).hexdigest()[:32]
            payload = {'event_id': event_id, 'turn_id': self.turn_id,
                       'kind': 'candidate' if item.role == 'user' else 'interviewer',
                       'text': text}
            try:
                self.queue.put_nowait(payload)
                self.seen.add(item.id)
            except asyncio.QueueFull:
                self._fail()

    def _fail(self):
        if not self.failed:
            self.failed = True
            self.on_failure()

    async def flush(self):
        if self.failed:
            raise RuntimeError('Transcript persistence is unavailable.')
        await asyncio.wait_for(self.queue.join(), timeout=10)

    async def _run(self):
        while True:
            payload = await self.queue.get()
            try:
                for attempt in range(self.attempts):
                    try:
                        await self.backend.call('event', **payload)
                        break
                    except (httpx.HTTPError, ValueError):
                        if attempt + 1 == self.attempts:
                            self._fail()
                            return
                        await asyncio.sleep(self.retry_delay)
            finally:
                self.queue.task_done()

    async def close(self):
        if self.worker and not self.failed:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=5)
            except asyncio.TimeoutError:
                self._fail()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
