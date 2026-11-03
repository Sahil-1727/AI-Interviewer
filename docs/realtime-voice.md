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
| `app/static/js/voice.js`, `voice-turns.js`, `conversation.js`, `interview-instructions.js` | Capture/playback, endpoint policy, conversation state and readiness checks |
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

The instruction screen requires acknowledgment and a successful microphone
access check to start voice mode. The optional camera check never blocks it;
manual mode is available if microphone access is unavailable.

Python owns speech recognition, synthesis and interview decisions:

1. Start claims a session-owned interview ID and returns its current turn ID.
2. The question is synthesized with edge-tts and played through one reused audio
   element, unlocked by the initial Start click. The mic is off during synthesis
   and playback, then opens after the configured speaker-tail interval.
3. The browser tentatively detects sustained sound. After a one-second pause it
   transcribes a checkpoint while continuing to record the next independent file,
   so a completion phrase can be recognized before the end-of-answer timer.
   Continuous speech also rotates files every 25 seconds.
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
| INITIAL_ANSWER_TIMEOUT_SECONDS | 10 | Mark an initially silent managed voice turn unanswered |
| INITIAL_THINKING_PROMPT_SECONDS | 15 | Gentle visual prompt in standalone automatic recording |
| END_OF_ANSWER_SILENCE_SECONDS | 5 | Finish automatically after this much silence following activity |
| ACTIVITY_GAP_SECONDS | 0.2 | Allow short level drops between observed sound while detecting speech |
| COMMAND_CHECKPOINT_SECONDS | 1 | Pause before sending a segment for early phrase recognition |
| PLAYBACK_SETTLE_SECONDS | 0.6 | Mic remains closed after question audio |
| SEGMENT_SECONDS | 25 | Bound each browser audio file, even for continuous speech |
| MAX_SEGMENT_SECONDS | 90 | Enforce a server decode limit incrementally |
| MAX_PENDING_SEGMENTS | 8 | Pause on STT backlog; preserve queued files plus the current file |
| MAX_CAPTURE_SECONDS | 600 | Pause and save instead of silently truncating a long answer |
| REQUEST_TIMEOUT_SECONDS | 90 | Client request bound |
| GEMINI_TIMEOUT_SECONDS | 60 | Single-attempt provider timeout, SDK retries disabled |
| CONVERSATION_STALE_SECONDS | 240 | Interrupt recovery threshold; never silently redispatch |
| WHISPER_BEAM_SIZE | 3 | English decoder beam size; higher values can increase CPU latency |

In managed voice mode, initial silence marks the turn unanswered after ten seconds;
it is not sent for evaluation. Pause the interview if more thinking time is needed.
Standalone/manual recording keeps its existing behavior. Once sustained activity has started,
five seconds without further detected activity finalizes the answer and submits a
valid transcript automatically. Short sound spikes below the minimum sustained
activity do not reset the timer. If a later segment is rejected as non-speech by
server VAD, the detector restores the last validated speech time when possible.
There is no long-pause confirmation or grace
period after speech. Select Pause if you need more than five seconds to think.

Whisper can recognize “I'm done” as a standalone utterance or a final separate
sentence; the preceding answer and `raw_transcript` are preserved. A confident
command finishes when the checkpoint transcription returns, without waiting for
the five-second timer. The default checkpoint itself waits one second of
silence, and Whisper processing adds latency, so this is not instantaneous speech
recognition. Other recognized controls include “That's all”, “Give me a moment”,
“Let me think”, and “Can you repeat the question?” A recognized more-time command
restarts the initial waiting period. Quoted or embedded phrases are left in
answer text; recognition can still misinterpret ambiguous speech. Done Speaking
is the immediate manual fallback. A backend guard prevents a control-only
transcript from being sent to Gemini.

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
resumed. End Interview asks for confirmation, immediately stops capture and
playback, and freezes the history of answers already evaluated at the server's
termination boundary. An evaluation already dispatched may finish externally, but
its uncommitted result is ignored. Only the frozen history is passed to the
scorecard generator; unanswered questions are not supplied as zero scores. With
no evaluated answers, the report displays insufficient evidence without calling
Gemini. Early scorecards are cached, and refresh or repeated End requests do not
regenerate them. A failed scorecard can be retried explicitly. Use manual mode to
return to the original HTML flow before termination.

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

Open `http://127.0.0.1:5001/voice-demo`, check microphone access and acknowledge
the instructions, then click Start Interview. The camera check is optional. Answer two main questions and one follow-up. Check initial
10–15-second initial thinking time, a pause shorter than five seconds followed
by resumed speech, automatic completion at five seconds, standalone commands,
pause/repeat, early termination and final scorecard. Use manual mode to check
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
END_OF_ANSWER_SILENCE_SECONDS=1.5 COMMAND_CHECKPOINT_SECONDS=.4 \
MIN_ACTIVITY_SECONDS=.1 PLAYBACK_SETTLE_SECONDS=.05 \
INITIAL_THINKING_PROMPT_SECONDS=2 POLL_INTERVAL_MS=50 \
PYTHONPATH=. venv/bin/python tests/voice_demo.py
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

### Earlier real-service measurement, November 2, 2026

The controlled real-service result below was measured on the previous 10-second
confirmation and two-second grace implementation; it does not verify the current
five-second endpoint. No real Gemini calls were made for the latest change.

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
mocked providers and must not be represented as production performance. These
historical endpoint measurements do not apply to the current five-second policy.

For the current policy, mock Chromium tests with synthetic audio observed a
5,002 ms automatic endpoint and a 1,059 ms spoken-command checkpoint. The latter
uses a mocked transcription response and excludes real Whisper latency. No Gemini
API calls were made for the current update.

`window.voiceMetrics` records endpoint, browser upload, STT roundtrip/server time,
TTS request/generation/playback-start and end-of-speech-to-next-playback timings.
Backend completed state/journals retain evaluation and scorecard times. Upload
completion is a browser transport event, not an independently isolated uplink
benchmark. Server STT time includes decode, model access/lock wait and inference.

## Deployment considerations and remaining limits

- Require HTTPS or localhost, MediaRecorder, Web Audio and microphone permission.
  Chromium is tested. Safari/iOS autoplay, codecs, actual microphones, room echo,
  fan/keyboard noise and a wider range of accents need human-device validation.
- The browser uses an audio-level heuristic, not a real-time speech classifier.
  Brief sounds below the minimum activity duration do not reset the silence timer,
  but sustained fan or room noise can still be mistaken for speech or delay
  completion. Whisper's server-side Silero VAD filters empty segments; it cannot
  guarantee correct live silence detection. Quiet speech may need a lower
  threshold or Done Speaking. No claim
  of semantic completion or interruption/barge-in support is made.
- Keep `SECRET_KEY` private and configure private session/journal directories.
  Secure cookies, authentication, CSRF policy, rate limits and TLS belong in the
  deployment setup. There is no production authentication layer added here.
- Use a threaded production WSGI server, not Flask's dev server. Model memory and
  the thread-pool limit are per worker. Bound reverse-proxy upload/request times
  consistently with the configured 12 MB upload, 90s audio segment and timeouts.
- File journal snapshots expire opportunistically on new interview starts after the
  configured 24-hour retention. Stable lock files remain; arrange offline cleanup
  while the service is stopped. Sessions retain their existing cache lifecycle.
- Without `DATABASE_URL`, do not put the file journal on an unverified network
  filesystem or multiple hosts; its POSIX locks target macOS/Linux. With
  `DATABASE_URL`, new voice journals use PostgreSQL row locks. Flask sessions
  and manual interviews remain file-backed, so multi-host operation is not yet
  supported. See `docs/postgresql-integration.md` for setup and limitations.
