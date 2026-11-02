# AI Interviewer

A Flask application for interview practice using a job description and PDF resume.
Google Gemini generates personalized questions, evaluates answers, and produces a
final scorecard.

## Current features

- PDF resume upload and text extraction with `pypdf`; resumes must contain
  extractable text (OCR for scanned PDFs is not implemented).
- Personalized technical, project, problem-solving, and behavioral questions.
- Automated answer evaluation with scores from 0–10 and adaptive follow-ups.
- Typed answers or microphone recordings transcribed with `faster-whisper`.
  Transcripts remain editable before submission; transcription currently uses English.
- On-demand question audio through `edge-tts`, with playback and stop controls.
- Optional experimental voice turns with patient pauses, automatic mic handling,
  editable transcripts and manual completion; see the Stage 1 section below.
- Optional camera preview. Video is displayed live in the browser only; the
  application does not record, store, or analyze video.
- Redesigned, responsive setup, interview, and report pages with labeled controls,
  keyboard focus indicators, status messages, and reduced-motion support.
- Final scorecards with overall and competency scores, strengths, areas to improve,
  technical and communication assessments, a recommendation, and a printable report.

## Interview flow

The interview is turn-based:

1. Enter a candidate name and job description, then upload a PDF resume.
2. Gemini creates an interview plan; the prompt requests 10–15 questions.
3. Read or play the current question. Type an answer, or record and stop audio,
   then review and edit the transcript before submitting.
4. Gemini evaluates the submitted answer. If a follow-up is needed, answer it
   before advancing to the next main question. Follow-up answers are saved with
   the original answer for the final assessment.
5. After the last main question and any follow-up, view the final scorecard.

Audio recording and transcription happen per answer. Experimental automatic
capture is available, while answer submission and interview progression remain
manual. Microphone and camera access require browser permission; both are optional
for typed interviews.

## Installation and startup

From the project root, with Python 3 and pip available (macOS/Linux):

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Set the required variable in `.env` using your own Gemini API key:

```dotenv
GEMINI_API_KEY=your_gemini_api_key_here
```

Optional: set `WHISPER_MODEL` to choose the transcription model (default: `base`).
Transcription runs locally on the CPU; the first use may download model files.
Gemini requests and question audio generation require internet access.

Start the application:

```bash
venv/bin/python run.py
```

Open <http://127.0.0.1:5000>. This command runs the Flask development server with
debug mode enabled.

## Session storage

Flask-Session uses CacheLib filesystem storage in `instance/sessions/`, created
at startup and ignored by Git. Interview plans, answers, evaluations, and progress
stay on the server; the browser cookie contains only a session ID, avoiding the
cookie size limit as interview history grows.

After upgrading from cookie-based sessions, restart the application and **start a
new interview**. Existing browser-cookie interview data is not migrated.
This local session storage is not database integration.

## Mocked regression tests

With dependencies installed and the environment configured, run:

```bash
venv/bin/python -m unittest discover -s tests -v
```

`tests/test_interview_flow.py` mocks resume extraction and Gemini operations. It
checks completion with large answers across 10- and 15-question interviews,
follow-ups, cookie size, completed-session redirects, missing follow-ups, and
new-interview state reset. These tests do not make Gemini API calls.

The root-level `test_gemini.py`, `test_stt.py`, and `test_tts.py` are manual service
scripts, separate from this mocked suite; they may contact external services,
load models, or require local audio files.

## Project structure

```text
AI Interviewer/
├── run.py                       # Active Flask app, routes, and session configuration
├── requirements.txt
├── .env.example                 # Environment variable template
├── app/
│   ├── services/
│   │   ├── gemini.py            # Interview planning, evaluation, and scorecards
│   │   ├── resume_parser.py     # PDF text extraction
│   │   ├── stt.py               # Audio transcription
│   │   └── tts.py               # Question audio generation
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/                  # ui.js, voice.js, camera.js
│   ├── templates/               # Shared layout, components, setup, interview, scorecard
│   ├── routes/                  # Earlier blueprint; not registered by run.py
│   ├── models/                  # Placeholder package; no database models
│   └── prompts/                 # Placeholder package; prompts currently in gemini.py
├── tests/test_interview_flow.py # Mocked regression suite
├── test_gemini.py               # Manual service scripts
├── test_stt.py
├── test_tts.py
└── instance/sessions/           # Generated local session data; ignored by Git
```

The local `.env` and `venv/` are also ignored by Git.

## Planned work

- Database integration for persistent interview records (not implemented yet).
- Continuous, real-time voice interviews.
- Interview analytics.
- Production deployment configuration.

## Experimental voice turns — Stage 1

Implemented on `feature/realtime-voice-interview`. Automatic mode is **off by
default**. The existing typed/manual recording flow and camera preview remain
available. No voice action submits an answer, evaluates it, advances a question,
or requests a scorecard. Gemini still runs only through the existing manual
interview actions; Stage 2 automatic progression is deliberately unimplemented.

Enable **Automatic voice turns** to play the question and automatically open the
microphone after playback ends plus a short speaker-tail delay. **Listen / repeat
question** saves any captured answer and closes the microphone before synthesis
and playback. **Stop audio** pauses; it does not start recording. **Record answer**
also starts listening directly if you have already read the question.

The browser measures RMS audio energy. This is **not reliable speech VAD or
semantic end-of-turn recognition**. Sustained activity above the configured
threshold arms the silence policy; faster-whisper's server-side VAD still decides
whether the uploaded audio contains speech.

| Event | State / behavior |
| --- | --- |
| Preparing question audio | `thinking`; mic off |
| AI question playback | `speaking`; mic off |
| Playback finishes | `thinking`; open mic after the configurable tail delay |
| No first speech yet | Stay `thinking`; at 15 seconds show “Still thinking?”; never auto-finish initial silence |
| Sustained audio activity | `listening`; reset silence and confirmation timers |
| 5 seconds since activity | `thinking`; transcribe the completed audio segment once while recording the next segment |
| 10 seconds since activity | Show “Still thinking?” and a **further 10-second grace period** |
| Continue speaking / select Still thinking | Cancel the impending finish and continue listening |
| Grace expires after actual activity | `transcribing`, then `review`; release mic and prepare the editable transcript |
| Done Speaking | `transcribing` → `review`; overrides audio-level detection for quiet speech |
| Pause / hide tab | Release mic, save captured segments, then `paused` |
| Resume / record more in automatic mode | Start another capture and append to the existing edited transcript |
| Permission, device, playback or STT failure | `error`; release capture resources and keep already transcribed text |

Audio segments are separate MediaRecorder files, queued in order through the
existing `/api/transcribe` endpoint. They are never repeatedly uploaded as a
growing recording. Capture continues during checkpoint STT to avoid losing speech
to transcription latency. Final transcription blocks both keyboard and button
submission. Pause, navigation and late microphone-permission responses have
explicit cleanup; voice cleanup only stops audio tracks, never the separate
camera stream. A hidden tab pauses rather than relying on throttled timers.

Automatic requests add `detect_completion=true`. Whisper may recognize an
**isolated final segment** such as “I'm done”, “I am done”, “I have finished my
answer”, or “That's the end of my answer”. Only segments meeting the configured
log-probability and no-speech gates count; the command is removed from the answer.
Completion is checked at the next silence checkpoint, not while you are speaking.
A delayed command cannot stop you after new sound has resumed. Manual-mode
requests preserve their original transcript response and keep these words.

### Configuration

Copy the optional voice values from `.env.example` into `.env`, or export them,
then restart Flask. `app/voice_config.py` validates the settings and renders them
as JSON into the interview page. The requested defaults are:

```dotenv
INITIAL_THINKING_PROMPT_SECONDS=15
MID_ANSWER_PAUSE_SECONDS=5
LONG_PAUSE_CONFIRMATION_SECONDS=10
```

`TURN_CONFIRMATION_GRACE_SECONDS=10` provides the additional patient confirmation
window. Other exposed settings cover the RMS threshold, minimum activity duration,
sample interval, playback settling time, request timeout, capture duration limit,
and Whisper command confidence gates. The capture limit defaults to ten minutes;
reaching it pauses and saves the answer rather than submitting it. Lower the RMS
threshold for a quiet microphone; raise it if constant room noise falsely arms
the silence policy.

### Verify without Gemini calls

Run the deterministic suites (Node 18+ for native Blob/FormData):

```bash
node --test tests/voice_turns.test.cjs
venv/bin/python -m unittest discover -s tests -v
```

The JavaScript suite simulates microphone energy, clocks, permission responses,
MediaRecorder, TTS playback and STT latency/failures. Python tests mock Whisper
and Gemini, verify completion gating and the unchanged manual API contract, and
assert transcription cannot advance the session or call Gemini. Existing interview
flow regressions remain included. No suite downloads a Whisper model or calls
external services.

For manual **real microphone** testing with Gemini blocked:

```bash
PYTHONPATH=. venv/bin/python tests/voice_demo.py
```

Open <http://127.0.0.1:5001/voice-demo>. This separate demo process seeds a sample
question and blocks every non-voice POST plus scorecards before route execution.
Real faster-whisper and edge-tts still run: Whisper may download its model on
first use, and edge-tts requires internet. No production routes are added by the
demo script. Do not use `run.py` for this Gemini-blocked sandbox.

1. Leave automatic mode off. Type, record, select Done Speaking, and edit the
   transcript. Confirm no question advances without a manual Submit action.
2. Enable automatic mode. Verify the mic indicator is off while the AI speaks,
   then turns on automatically. Wait silently for at least 30 seconds: the
   thinking prompt should appear, with no automatic transcript or question change.
3. Explain something with pauses shorter than five seconds, then a pause of
   six to nine seconds. Continue your answer: it should remain the same turn.
4. Pause for ten seconds after speaking. Choose Still thinking during the grace
   window, then continue. Repeat and let the extra ten seconds elapse: the mic
   should close and an editable transcript should be ready, without submission.
5. Say an isolated “I'm done” after your explanation and pause. A sufficiently
   confident Whisper result should prepare the transcript. If the phrase is
   merged with the explanation or uncertain, use Done Speaking.
6. Pause/resume and repeat the question mid-answer. Confirm earlier transcript
   text is kept and speech can be appended. Turn the camera on and confirm it
   continues running through all voice actions.
7. Deny permission, test a quiet voice with Done Speaking, switch browser tabs,
   and navigate away/back. Check that capture stops, text stays editable, and
   returning requires Resume. In DevTools, verify each recorded segment sends
   one `/api/transcribe` request and no voice action calls `/submit-answer`.
8. In this sandbox, clicking Submit is intentionally blocked with HTTP 409 and
   cannot call Gemini. Reload `/voice-demo` to reset the fixture.

### Known limits

- Room noise can appear to be speech; quiet speech may be missed by RMS. The
  manual Done Speaking override always sends the current recording to Whisper.
- Whisper scores are heuristics, not calibrated intent confidence. An isolated
  quoted command can be misrecognized; many normal “I'm done” utterances merge
  into a longer segment and intentionally do not count as a command.
- Segment boundaries at long pauses can affect punctuation/context. Earlier
  successful segments survive STT errors; the failed segment is not automatically
  retried, and untranscribed audio is not retained after an error or navigation.
- Extremely slow local STT can delay transcript review. The mic keeps capturing
  until completion, pause, an error, or the capture limit; request timeouts release
  resources. Browser permission and autoplay restrictions may require a fresh
  Record/Resume click. HTTPS or localhost and MediaRecorder are required;
  automatic mode additionally needs Web Audio.
- Real-device echo, permissions, browser codecs and accuracy still need the
  manual checks above. The automated suites use simulated browser audio and
  mocked transcription. Stage 2 must wait for that verification.
