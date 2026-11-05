"""Mocked controller tests; no LiveKit, Gemini or speech service calls."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    from livekit_agent.interview_agent import InterviewController
except ModuleNotFoundError:
    InterviewController = None


@unittest.skipIf(InterviewController is None, 'requires optional LiveKit agent dependencies')
class LiveKitControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_uses_the_standalone_demo_models(self):
        from livekit_agent import interview_agent as worker
        with patch.object(worker.inference, 'STT') as stt, patch.object(worker.inference, 'LLM') as llm, \
             patch.object(worker.inference, 'TTS') as tts, patch.object(worker.inference, 'TurnDetector'), \
             patch.object(worker, 'TurnHandlingOptions'), patch.object(worker, 'AgentSession'):
            worker.create_session()
        stt.assert_called_once_with(model='assemblyai/universal-3-6-pro', language='en')
        llm.assert_called_once_with(model='google/gemma-4-31b-it')
        tts.assert_called_once_with(model='fishaudio/s2.1-pro', voice='fa4c9eb3dccc4806b382b40d61c6b10a')

    async def test_answer_evaluation_and_follow_up_use_backend_state(self):
        backend = AsyncMock()
        backend.call.side_effect = [
            {'status': 'evaluating'},
            {'status': 'ready', 'turn_id': 'b' * 32, 'question': {'question': 'Why?'}},
        ]
        say = AsyncMock()
        controller = InterviewController(backend, say, poll_seconds=0)
        await controller.present({'status': 'ready', 'turn_id': 'a' * 32, 'question': {'question': 'Explain Java.'}})
        await controller.submit('A programming language.')
        self.assertEqual(say.await_args_list[0].args[0], 'Explain Java.')
        self.assertEqual(say.await_args_list[1].args[0], 'Why?')
        self.assertEqual(backend.call.await_args_list[0].kwargs['turn_id'], 'a' * 32)
        self.assertEqual(backend.call.await_args_list[0].kwargs['answer'], 'A programming language.')

    async def test_duplicate_and_empty_turns_do_not_submit(self):
        backend = AsyncMock()
        gate = asyncio.Event()
        async def wait(*args, **kwargs):
            await gate.wait()
            return {'status': 'completed'}
        backend.call.side_effect = wait
        controller = InterviewController(backend, AsyncMock())
        controller.turn_id = 'a' * 32
        task = asyncio.create_task(controller.submit('Answer'))
        await asyncio.sleep(0)
        await controller.submit('Duplicate')
        gate.set()
        await task
        await controller.submit('')
        self.assertEqual(backend.call.await_count, 1)

    async def test_repeat_command_never_submits_or_creates_a_question(self):
        model = Mock()
        model.chat.return_value.collect = AsyncMock(return_value=SimpleNamespace(text='REPEAT'))
        backend = AsyncMock()
        backend.call.return_value = {'status': 'ready', 'turn_id': 'a' * 32, 'question': {'question': 'Explain Java.'}}
        say = AsyncMock()
        controller = InterviewController(backend, say, intent_model=model)
        controller.turn_id = 'a' * 32
        await controller.submit('Please repeat the question.')
        backend.call.assert_awaited_once_with('state')
        say.assert_awaited_once_with('Explain Java.')

    async def test_unavailable_intent_model_keeps_original_answer(self):
        model = Mock()
        model.chat.return_value.collect = AsyncMock(side_effect=TimeoutError())
        backend = AsyncMock()
        backend.call.return_value = {'status': 'completed'}
        controller = InterviewController(backend, AsyncMock(), intent_model=model)
        controller.turn_id = 'a' * 32
        await controller.submit('My original answer.')
        self.assertEqual(backend.call.await_args.kwargs['answer'], 'My original answer.')
