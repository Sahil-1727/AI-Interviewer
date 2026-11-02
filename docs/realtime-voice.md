# Conversational voice interview

All implementation commits are on `feature/realtime-voice-interview`. Manual
interview forms, Gemini prompts/scoring, follow-up accounting, edge-tts and webcam
preview remain available. Stage 1 capture has been integrated with automatic
progression. No runtime framework, database migration, WebRTC or WebSocket was
added.

## Changed files

| Files | Responsibility |
| --- | --- |
| `run.py`, `app/routes/conversation.py` | Conversation endpoints, shared manual progression, scorecard cache, voice config and request limits |
| `app/services/conversation.py`, `app/services/interview_flow.py` | Background jobs, durable turn claims and shared progression |
| `app/services/gemini.py` | Configurable model, lazy client, timeout and disabled automatic retries; prompts unchanged |
| `app/services/stt.py`, `app/services/voice_commands.py` | Bounded decoding, real VAD evidence, confidence gates and standalone commands |
| `app/voice_config.py`, `.env.example` | Central thresholds, durations, queue limits and model settings |
| `app/static/js/voice.js`, `voice-turns.js`, `conversation.js` | Capture/playback lifecycle, endpoint policy, explicit state graph and continuous UI loop |
| `app/static/js/camera.js` | Release late camera permissions after navigation |
| `app/templates/interview.html`, `app/static/css/style.css` | Conversational controls, actual mic activity meter and preserved manual fallback |
| `tests/test_conversation.py`, `test_voice_audio.py`, `test_voice_stage1.py`, `voice_turns.test.cjs` | Backend, audio, manual-contract and simulated browser regressions |
| `tests/browser_voice.cjs`, `voice_demo.py`, `real_voice_smoke.py` | Chromium scenarios, quota-free mock decisions and opt-in real-service test |
| `.gitignore`, `README.md`, this guide | Local artifact ignores, setup, measured results and limitations |

## Architecture and flow

The browser uses MediaRecorder/Web Audio and HTTP. `voice.js` owns microphone
resources, independent bounded audio files, sequential STT uploads and playback.
`voice-turns.js` implements the deterministic silence policy. `conversation.js`
owns a checked state-transition graph, question rendering, API recovery and the
continuous interview loop. Questions change in place; the final scorecard is the
only automatic page navigation. Camera streams are separate and stay active.

Python owns speech recognition, synthesis and interview decisions:

1. Start claims a session-owned interview ID and returns its current turn ID.
2. The question is synthesized with edge-tts and played through one reused audio
   element, unlocked by the initial Start click. The mic is off during synthesis
   and playback, then opens after the configured speaker-tail interval.
3. The browser tentatively detects sustained sound. Five seconds after activity
   it transcribes a checkpoint while continuing to record the next independent
   file. Continuous speech also rotates files every 25 seconds.
4. faster-whisper runs its bundled **real Silero VAD**, then decodes English speech.
   Automatic transcripts must pass voiced-duration and decoder-quality gates.
5. A completed, nonempty transcript is sent with a unique request ID and the
   server-issued turn ID. A bounded worker evaluates it with the existing Gemini
   function and runs shared Python progression rules.
6. A follow-up is attached to the original history entry. As in the original
   manual flow, its answer is retained as scorecard evidence; it is not separately
   scored and cannot generate an infinite chain of follow-ups.
7. Polling returns the next question; it updates and plays automatically. The
   last answer triggers scorecard generation in the worker. The cached scorecard
   is rendered through the existing `/final-scorecard` route.

RMS audio energy is **not speech or semantic completion recognition**. The browser
adapts a noise-floor gate conservatively; actual VAD and transcription validation
happen on the server. This is HTTP endpointing, not streaming token generation.

## Patient timing and commands

Settings are validated in `app/voice_config.py`, overridable through `.env` or
exported environment variables. Restart Flask after changing them.

| Setting | Default | Meaning |
| --- | ---: | --- |
| INITIAL_THINKING_PROMPT_SECONDS | 15 | Gentle visual prompt before first speech; initial silence never auto-submits |
| MID_ANSWER_PAUSE_SECONDS | 5 | Checkpoint; a five-second pause does not evaluate an answer |
| LONG_PAUSE_CONFIRMATION_SECONDS | 10 | Offer “Still thinking?” after actual activity |
| TURN_CONFIRMATION_GRACE_SECONDS | 10 | Additional chance to continue before automatic completion |
| PLAYBACK_SETTLE_SECONDS | 0.6 | Mic remains closed after question audio |
| SEGMENT_SECONDS | 25 | Bound each browser audio file, even for continuous speech |
| MAX_SEGMENT_SECONDS | 90 | Enforce a server decode limit incrementally |
| MAX_PENDING_SEGMENTS | 8 | Pause on STT backlog; preserve queued files plus the current file |
| MAX_CAPTURE_SECONDS | 600 | Pause and save instead of silently truncating a long answer |
| REQUEST_TIMEOUT_SECONDS | 90 | Client request bound |
| GEMINI_TIMEOUT_SECONDS | 60 | Single-attempt provider timeout, SDK retries disabled |
| CONVERSATION_STALE_SECONDS | 240 | Interrupt recovery threshold; never silently redispatch |

Defaults deliberately favor patience: automatic silence completion is about
**20 seconds after last activity**, not five. A phrase command can finish at a
checkpoint sooner. The “Still thinking” button renews the wait. A recognized
“Give me a moment” or “Let me think” disarms completion until fresh speech.

Command recognition requires a confident **whole uploaded utterance**, never a
substring of a technical answer. Supported completion phrases include “I'm done”,
“I am done”, and “That's all”; repeat phrases include “Can you repeat the question?”
and “Please repeat the question.” No phrase is removed from a larger answer, and
`raw_transcript` is preserved in the automatic STT response. For best recognition,
use a standalone command after a pause. A command merged into a longer answer is
intentionally treated as answer content. Confidence scores are heuristics, not a
proof of intent. If recognition is uncertain, silence completion and Done
Speaking remain available. A backend guard also prevents a control-only transcript
from being sent to Gemini.

The frontend state graph explicitly controls `IDLE`, `PREPARING`, `AI_SPEAKING`,
`THINKING`, `LISTENING`, `POSSIBLE_END_OF_TURN`, `TRANSCRIBING`, `EVALUATING`,
`PAUSED`, `RECOVERING`, `COMPLETED` and `ERROR`. Speech resets endpoint timers;
late STT commands cannot finish a turn after fresh activity. Invalid state jumps
throw, and the tested recovery paths pass through explicit pause/recovery states.

## HTTP protocol and idempotency

| Endpoint | Contract |
| --- | --- |
| POST /api/conversation/start | Idempotently initialize from the existing Flask interview session |
| GET /api/conversation | Current question/progress, paused/error/job state and timings; no inference |
| POST /api/conversation/answer | `{interview_id, turn_id, request_id, answer}`; returns 202 and structured state |
| POST /api/conversation/retry | Explicit new processing attempt after a saved error; same payload shape |
| POST /api/conversation/control | `{action: pause/resume/manual/end}` |
| POST /api/transcribe | Existing audio upload; optional `detect_completion=true` adds command/VAD metadata |
| POST /api/synthesize | Existing text-to-audio endpoint; includes measured TTS Server-Timing |
| GET /final-scorecard | Existing report; automatic interviews use the worker's cached scorecard |

The request body cannot select another session's interview or enable mock mode.
Empty/oversized answers, stale turn IDs and invalid transitions are rejected. A
repeated request ID with identical content returns state without re-evaluation;
changing its content is rejected. Different IDs racing on one turn cannot claim
it twice. A full answer is saved before processing. Workers save evaluated
history before requesting the scorecard, so scorecard recovery does not repeat
the last evaluation. Late/out-of-order browser state responses are ignored.

The temporary journal uses standard-library file locks and atomic, private JSON
snapshots in `instance/conversations/`. It survives restarts and coordinates
workers sharing the **same local filesystem on one host**. No lock is held during
Gemini inference. A local bounded thread pool keeps evaluation/scorecard work out
of HTTP response handling. Transcription still occupies its request thread and
is serialized around the reused local model; run a threaded WSGI server.

Exactly-once external API execution cannot be guaranteed across a process crash
or an ambiguous provider timeout. Such jobs become recoverable errors and are
**never automatically retried**. An explicit Retry processing action authorizes a
new attempt and may incur another provider call; history progression remains
single-commit. SDK retries are disabled. Queue admission is per process; the
journal's per-turn exclusion works across same-host workers.

## Recovery and privacy

Pause closes the mic immediately and saves capture before waiting for STT. Resume
appends to the saved draft. Repeat saves partial speech before replaying the same
question. Tab hiding pauses to avoid timer throttling masquerading as silence.
Permission failures restore typing/manual fallback. TTS failures retain the text
question and allow a retry. Empty/unrecognizable speech stays on the same question
without Gemini. STT failures keep completed text and untranscribed audio in memory
for an explicit retry in capture order. A backlog or duration cap pauses rather
than silently discarding the rest of an answer.

Accepted answers and progress live in the server journal. Confirmed local drafts
and pending idempotency keys live in tab-scoped sessionStorage. Refresh returns
paused and requires Resume to satisfy browser audio permissions. A lost answer
HTTP response is reconciled with server state before resending the same key.
Server evaluation may finish while paused; it cannot trigger playback until
resumed. End stops future turns; it does not claim to cancel an already-dispatched
external request. Use manual mode to return to the original HTML flow.

Raw audio is held in the browser and private server temporary files, deleted on
success or error. It is not written into conversation journals. **Untranscribed
in-memory audio is lost if the tab closes/reloads or the process dies**; confirmed
text and server-accepted answers survive. No video is recorded or analyzed. The
camera epoch guard also releases late permission responses after navigation.

## Verification

```bash
venv/bin/python -m unittest discover -s tests -v
node --test tests/voice_turns.test.cjs
```

Python tests cover shared manual regressions, complete automatic progression,
follow-up association, concurrent same/different request IDs, expired processing,
explicit retry, scorecard-only retry, control-only transcripts, session ownership,
real bounded audio decoding/temp cleanup and the bundled Silero VAD rejecting
silence, including independent processes sharing a durable turn claim.
Whisper/Gemini inference is mocked in ordinary tests. Node tests simulate
speech/silence, initial thinking, natural pauses, resumed speech, completion,
command waiting, resource cancellation, duplicate playback callbacks, long-answer
segmentation, bounded backlog and retrying saved audio. No ordinary test consumes
Gemini quota or downloads a Whisper model.

For a manual microphone test with deterministic Gemini decisions:

```bash
PYTHONPATH=. venv/bin/python tests/voice_demo.py
```

Open `http://127.0.0.1:5001/voice-demo`, optionally turn on the camera, and click
Start Interview. Answer two main questions and one follow-up. Check initial
10–15-second thinking time, five-second mid-answer pauses, long-pause continuation,
standalone commands, pause/repeat and final scorecard. Use manual mode to check
record/stop/submit fallback. Real STT/TTS still run in this demo.

`tests/browser_voice.cjs` uses real Chromium, MediaRecorder and Web Audio with
synthetic camera/mic devices. It mocks only STT/TTS HTTP and uses the server's
deterministic Gemini implementation. It verifies one Start click through the
scorecard, camera-track continuity, no mic/TTS overlap, TTS/STT recovery, lost
answer responses, denied permission, empty transcription and pause/repeat/refresh.
Install Playwright as a test-only dependency and provide Chrome (or a downloaded
Playwright Chromium); runtime dependencies are unchanged. For a quick fixture,
launch the mock server with:

```bash
MID_ANSWER_PAUSE_SECONDS=.4 LONG_PAUSE_CONFIRMATION_SECONDS=.8 \
TURN_CONFIRMATION_GRACE_SECONDS=.5 MIN_ACTIVITY_SECONDS=.1 \
PLAYBACK_SETTLE_SECONDS=.05 INITIAL_THINKING_PROMPT_SECONDS=2 \
POLL_INTERVAL_MS=50 PYTHONPATH=. venv/bin/python tests/voice_demo.py
```

Then run `node tests/browser_voice.cjs`. Set `NODE_PATH` if Playwright is installed
outside the repo and `CHROME_EXECUTABLE` if using a system Chrome. Generated
reports/screenshots are ignored under `test-results/`.

A separate **opt-in, quota-consuming** real-service check is available in
`tests/real_voice_smoke.py` and the same browser driver with `VOICE_REAL_SMOKE=1`,
`VOICE_TEST_URL=http://127.0.0.1:5002` and
`VOICE_REAL_AUDIO=test-results/real-candidate.wav`. It seeds one planned question,
generates synthetic candidate speech using edge-tts, and uses real Whisper,
Gemini evaluation and scorecard generation. It is excluded from test discovery.

### Measured on this checkout, November 2, 2026

29 Python tests, 26 JavaScript tests and eight mock Chromium scenarios passed.
The duplicate-callback browser case was also run as a targeted regression.

A scripted real-service Chromium interview passed with one Start interaction,
one planned question, two Whisper segments and one automatic scorecard. It used
synthetic candidate speech, not a human microphone. Gemini made one evaluation
and one scorecard call. The speaker/mic exclusion and camera continuity assertions
passed. The real check used 5s midpoint / 10s long pause / **2s test grace**.

| Measured stage | Observed time |
| --- | ---: |
| edge-tts question generation | 1,474 ms |
| Browser playback start after play request | 7.4 ms |
| Browser audio upload-complete events | 120 / 118 ms |
| Full upload + STT round trips | 1,070 / 627 ms |
| Server transcription requests | 1,043 / 600 ms |
| Gemini main-answer evaluation | 1,870 ms |
| Gemini scorecard | 2,029 ms |
| Silence endpoint wait with 10s + 2s test grace | 12,104 ms |

These are one-run observations, not a latency guarantee. The real one-question
fixture has no next-question playback, so it does not establish a real-service
end-of-speech-to-next-playback number. Mock browser progression measured about
1.4–1.5 seconds with accelerated .8s + .5s endpoint settings; those numbers include
mocked providers and must not be represented as production performance.

`window.voiceMetrics` records endpoint, browser upload, STT roundtrip/server time,
TTS request/generation/playback-start and end-of-speech-to-next-playback timings.
Backend completed state/journals retain evaluation and scorecard times. Upload
completion is a browser transport event, not an independently isolated uplink
benchmark. Server STT time includes decode, model access/lock wait and inference.

## Deployment considerations and remaining limits

- Require HTTPS or localhost, MediaRecorder, Web Audio and microphone permission.
  Chromium is tested. Safari/iOS autoplay, codecs, actual microphones, room echo,
  fan/keyboard noise and a wider range of accents need human-device validation.
- Energy gates and Whisper confidence are imperfect. Quiet speech may need a
  lower threshold or Done Speaking; constant noise can arm the browser gate but
  empty/low-quality server transcription cannot advance the interview. No claim
  of semantic completion or interruption/barge-in support is made.
- Keep `SECRET_KEY` private and configure private session/journal directories.
  Secure cookies, authentication, CSRF policy, rate limits and TLS belong in the
  deployment setup. There is no production authentication layer added here.
- Use a threaded production WSGI server, not Flask's dev server. Model memory and
  the thread-pool limit are per worker. Bound reverse-proxy upload/request times
  consistently with the configured 12 MB upload, 90s audio segment and timeouts.
- Journal snapshots expire opportunistically on new interview starts after the
  configured 24-hour retention. Stable lock files remain; arrange offline cleanup
  while the service is stopped. Sessions retain their existing cache lifecycle.
- Do not put this journal on an unverified network filesystem or multiple hosts.
  Its POSIX file locking targets macOS/Linux; Windows needs a lock adapter.
  For horizontal scale, replace it with PostgreSQL transactional turn/job claims
  and a durable worker queue. There is no database migration in this branch.
