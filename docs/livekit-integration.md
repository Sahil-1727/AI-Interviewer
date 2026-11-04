# LiveKit integration: work in progress

The LiveKit interview mode is **not complete** and must remain disabled. The
existing Deepgram and Whisper interview flows remain the supported modes.

The first implementation step adds an opt-in
`POST /api/conversation/livekit/session` endpoint. It uses the existing browser
session and interview ownership checks, validates interview state, and issues a
five-minute room-scoped token with an explicit agent dispatch configuration. The
agent metadata contains the interview identifier only. Tokens and credentials
are not logged or stored in the database. This endpoint requires
`LIVEKIT_ENABLED=1`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`;
leave `LIVEKIT_ENABLED` unset in production while the integration is incomplete.

Still required before use:

- A separately deployed LiveKit agent using Deepgram and the existing interview
  engine through an authenticated, interview-scoped backend interface.
- Browser WebRTC audio, transcripts, interruption handling and controls, with
  the existing voice mode retained as a fallback.
- Durable dispatch/reconnect and duplicate-submission handling across workers,
  secure worker authorization, and complete interview lifecycle tests.
- Live validation against an isolated LiveKit project and test database; latency,
  resource use, hosting and deployment have not been verified.

Current [LiveKit dispatch documentation](https://docs.livekit.io/agents/server/agent-dispatch/)
states that token-based dispatch applies when a room is created. Therefore
issuing another token for an existing room does not guarantee redispatch after
an agent disconnect. Reconnection and recovery require further implementation.
LiveKit offers provider plugins for [Deepgram STT](https://docs.livekit.io/agents/models/stt/deepgram/)
and [Deepgram TTS](https://docs.livekit.io/agents/models/tts/deepgram/), allowing
the existing provider account to be used. LiveKit Inference is a separately billed
option. Check [current pricing](https://livekit.io/pricing) and account limits
before deployment.
