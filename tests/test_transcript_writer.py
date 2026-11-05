import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

try:
    from livekit.agents.llm import ChatMessage
    from livekit_agent.transcript_writer import TranscriptWriter
except ModuleNotFoundError:
    TranscriptWriter = None


class Session:
    def on(self, name):
        def register(fn):
            self.listener = fn
            return fn
        return register


@unittest.skipIf(TranscriptWriter is None, 'requires LiveKit agent dependencies')
class TranscriptWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_deduplication_and_retries(self):
        backend = SimpleNamespace(call=AsyncMock(side_effect=[httpx.ConnectError('offline'), {}, {}]))
        failure = Mock()
        writer = TranscriptWriter(backend, failure, retry_delay=0)
        self.addAsyncCleanup(writer.close)
        session = Session()
        writer.start(session)
        question = ChatMessage(id='q1', role='assistant', content=['Explain Python.'])
        answer = ChatMessage(id='a1', role='user', content=['A programming language.'])
        session.listener(SimpleNamespace(item=question))
        session.listener(SimpleNamespace(item=question))
        session.listener(SimpleNamespace(item=answer))
        await asyncio.wait_for(writer.queue.join(), timeout=2)
        calls = backend.call.await_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs, calls[1].kwargs)
        self.assertEqual(calls[1].kwargs['turn_id'], calls[2].kwargs['turn_id'])
        self.assertEqual(calls[2].kwargs['kind'], 'candidate')
        failure.assert_not_called()

    async def test_storage_failure_notifies_once(self):
        backend = SimpleNamespace(call=AsyncMock(side_effect=httpx.ConnectError('offline')))
        failure = Mock()
        writer = TranscriptWriter(backend, failure, attempts=2, retry_delay=0)
        self.addAsyncCleanup(writer.close)
        session = Session()
        writer.start(session)
        session.listener(SimpleNamespace(item=ChatMessage(id='a', role='user', content=['Answer.'])))
        await asyncio.wait_for(writer.queue.join(), timeout=2)
        self.assertEqual(backend.call.await_count, 2)
        failure.assert_called_once()
