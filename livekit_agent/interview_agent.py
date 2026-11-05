"""Separate LiveKit worker; Flask owns interview state and Gemini evaluation."""
import asyncio
import json
import logging
import os
import time
from uuid import uuid4

import httpx
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, StopResponse, TurnHandlingOptions, cli, inference, llm

from livekit_agent.config import LLM_MODEL, STT_MODEL, TTS_MODEL, TTS_VOICE

log = logging.getLogger('interview_agent')
server = AgentServer()


class InterviewBackend:
    def __init__(self, base_url, secret, ticket, client=None):
        if not base_url.startswith('https://') and not base_url.startswith('http://127.0.0.1:'):
            raise ValueError('Backend URL must use HTTPS or a local test address')
        if len(secret) < 32:
            raise ValueError('LIVEKIT_AGENT_SHARED_SECRET must be at least 32 characters')
        self.base = base_url.rstrip('/') + '/api/conversation/livekit/agent/'
        self.secret, self.ticket = secret, ticket
        self.client = client or httpx.AsyncClient(timeout=30)

    async def call(self, action, **data):
        response = await self.client.post(self.base + action, json={'ticket': self.ticket, **data},
                                          headers={'Authorization': 'Bearer ' + self.secret})
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.client.aclose()


class InterviewController:
    def __init__(self, backend, say, poll_seconds=0.5, intent_model=None):
        self.backend, self.say, self.poll_seconds = backend, say, poll_seconds
        self.intent_model = intent_model
        self.lock = asyncio.Lock()
        self.turn_id = None
        self.turn_completed_at = None

    async def present(self, state):
        if state.get('status') == 'completed':
            await self.say('Your interview is complete. Your scorecard is ready.')
            return
        if state.get('status') != 'ready' or state.get('paused') or state.get('end_requested'):
            return
        question = state.get('question')
        if question and question.get('question'):
            self.turn_id = state['turn_id']
            await self.say(question['question'])

    async def submit(self, text):
        text = (text or '').strip()
        if not text or self.lock.locked() or not self.turn_id:
            return
        async with self.lock:
            turn_id = self.turn_id
            self.turn_id = None
            request_id = uuid4().hex
            try:
                if self.intent_model and await self.is_repeat_request(text):
                    await self.present(await self.backend.call('state'))
                    return
                state = await self.backend.call('answer', turn_id=turn_id, request_id=request_id, answer=text)
                while state.get('status') in {'evaluating', 'scoring', 'ending'}:
                    await asyncio.sleep(self.poll_seconds)
                    state = await self.backend.call('state')
                evaluation_ms = state.get('timings', {}).get('evaluation_ms')
                if isinstance(evaluation_ms, (int, float)):
                    log.info('voice_metric stage=gemini_evaluation ms=%.1f', evaluation_ms)
                if state.get('status') == 'error':
                    await self.say('There was a problem processing your answer. Please use the interview page to retry.')
                    return
                await self.present(state)
            except (httpx.HTTPError, ValueError):
                log.warning('Interview backend request failed.')
                await self.say('The interview connection was interrupted. Please reconnect on the interview page.')

    async def is_repeat_request(self, text):
        """Let Gemma interpret a standalone repeat request; never rewrite answers."""
        context = llm.ChatContext()
        context.add_message(role='system', content=(
            'Reply with exactly REPEAT or ANSWER. REPEAT only if the entire user utterance '
            'asks for the current question to be repeated and contains no answer. '
            'For any answer, uncertainty, or mixed content, reply ANSWER.'))
        context.add_message(role='user', content=text)
        try:
            response = await asyncio.wait_for(self.intent_model.chat(chat_ctx=context).collect(), timeout=5)
            return response.text.strip().upper() == 'REPEAT'
        except Exception:
            # Preserve the raw response and leave scoring to the backend.
            log.warning('Voice intent interpretation unavailable; preserving answer.')
            return False


class InterviewAgent(Agent):
    def __init__(self, controller):
        super().__init__(instructions='Speak only the interview questions provided by the application.')
        self.controller = controller

    async def on_user_turn_completed(self, turn_ctx, new_message):
        self.controller.turn_completed_at = time.monotonic()
        asyncio.create_task(self.controller.submit(new_message.text_content))
        raise StopResponse()


def create_session():
    # Flask still selects every question; StopResponse prevents the LLM from
    # generating an unapproved interview question after a finalized answer.
    return AgentSession(
        stt=inference.STT(model=STT_MODEL, language='en'),
        llm=inference.LLM(model=LLM_MODEL),
        tts=inference.TTS(model=TTS_MODEL, voice=TTS_VOICE),
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector(), interruption={'enabled': True}),
    )


@server.rtc_session(agent_name='ai-interviewer')
async def entrypoint(ctx: JobContext):
    metadata = json.loads(ctx.job.metadata)
    ticket = metadata['ticket']
    backend = InterviewBackend(os.environ['INTERVIEW_BACKEND_URL'], os.environ['LIVEKIT_AGENT_SHARED_SECRET'], ticket)
    agent_mode = os.getenv('LIVEKIT_AGENT_MODE', 'conversational').strip().lower()
    if agent_mode not in {'conversational', 'flask'}:
        raise ValueError('LIVEKIT_AGENT_MODE must be conversational or flask.')
    if agent_mode == 'conversational':
        from livekit_agent.conversational_agent import ConversationalInterviewAgent, create_conversational_session
        from livekit_agent.transcript_writer import TranscriptWriter
        context = await backend.call('context')
        session = create_conversational_session(context)
        transcript_writer = TranscriptWriter(backend, lambda: ctx.shutdown('transcript persistence failed'))
        agent = ConversationalInterviewAgent(context)
    else:
        session = create_session()
        transcript_writer = None
        agent = None

    async def say(text):
        await session.say(text, allow_interruptions=True)

    controller = InterviewController(backend, say, intent_model=session.llm)

    async def enforce_session_lifetime():
        while True:
            await asyncio.sleep(5)
            try:
                state = await backend.call('state')
                if state.get('end_requested') or state.get('status') in {'ended', 'completed'}:
                    ctx.shutdown('interview ended')
                    return
            except (httpx.HTTPError, ValueError):
                # Stop provider audio when authorization or the backend fails.
                ctx.shutdown('interview authorization unavailable')
                return

    @session.on('agent_state_changed')
    def on_agent_state(event):
        if event.new_state == 'speaking' and controller.turn_completed_at is not None:
            elapsed = (time.monotonic() - controller.turn_completed_at) * 1000
            controller.turn_completed_at = None
            log.info('voice_metric stage=turn_to_server_speaking ms=%.1f', elapsed)

    @ctx.room.on('data_received')
    def on_control(packet):
        expected = 'candidate-' + ctx.room.name.removeprefix('interview-').split('-', 1)[0]
        if not packet.participant or packet.participant.identity != expected or packet.topic != 'interview.control' or len(packet.data) > 128:
            return
        try:
            message = json.loads(packet.data)
        except (ValueError, UnicodeDecodeError):
            return
        if message == {'action': 'done'}:
            asyncio.ensure_future(session.commit_user_turn())
        elif message == {'action': 'pause'}:
            asyncio.ensure_future(session.interrupt())
        elif message == {'action': 'resume'}:
            async def resume():
                if agent_mode == 'flask':
                    await controller.present(await backend.call('state'))
                else:
                    await session.generate_reply(instructions=(
                        'The candidate has resumed. Continue naturally from the conversation so far. '
                        'If your last question was cut off, repeat it; otherwise invite the candidate '
                        'to continue without repeating completed questions.'))
            asyncio.create_task(resume())
        elif message == {'action': 'finalize'} and transcript_writer:
            async def finalize():
                try:
                    await session.commit_user_turn()
                    await session.interrupt()
                    await transcript_writer.flush()
                    await backend.call('finalize')
                    await ctx.room.local_participant.publish_data(
                        json.dumps({'action': 'finalize-ready'}), reliable=True,
                        destination_identities=[expected], topic='interview.control.ack')
                except Exception:
                    log.warning('LiveKit transcript finalization could not be flushed.')
            asyncio.create_task(finalize())

    if transcript_writer:
        transcript_writer.start(session)
    await session.start(agent=agent or InterviewAgent(controller), room=ctx.room)
    monitor = asyncio.create_task(enforce_session_lifetime())
    try:
        if agent_mode == 'flask':
            await controller.present(await backend.call('state'))
        await asyncio.Event().wait()
    finally:
        monitor.cancel()
        if transcript_writer:
            await transcript_writer.close()
        await backend.close()


if __name__ == '__main__':
    cli.run_app(server)
