# AI Interviewer

An AI interview practice application built with Flask. It uses a candidate's job
description and PDF resume to create questions, conduct an interview, and produce
an evidence-based scorecard. The `feature/free-deployment` branch includes
conversational voice interviews, Deepgram, and PostgreSQL support.

## Features

- Generate a plan of up to 10 main questions with Google Gemini, prioritizing the
  job description and keeping resume questions high-level. Adaptive follow-ups
  can add questions during the interview.
- Practice in manual mode with typed answers or use the hands-free conversational
  voice mode. Manual audio recording uses faster-whisper; typed answers work with
  either speech provider.
- Listen to questions, repeat them, pause and resume, say “I'm done,” or use
  **Done Speaking**. Commands depend on speech recognition and may not always be
  recognized.
- Review pre-interview instructions and check the microphone. The camera is
  optional and provides a browser preview only; video is not recorded or analyzed.
- View a final scorecard, or end early and receive a provisional result based on
  completed evaluations. Unanswered questions are tracked separately; if there
  are no evaluated answers, the result reports insufficient evidence.
- Store interviews and sessions in PostgreSQL when configured. The authenticated
  session can export its interview transcript as JSON.

The interview is turn-based rather than a full-duplex conversation. In automatic
voice mode, the default is 10 seconds to begin speaking and 5 seconds of silence
after recognized speech to finish an answer. Pause when you need more time.
These thresholds are configurable; see `.env.example`. The microphone is closed
while the AI question is playing.

## Technology

- **Application:** Python, Flask, Flask-Sock, Gunicorn, HTML, CSS, JavaScript.
- **Interview planning and evaluation:** Google Gemini.
- **Speech recognition:** local faster-whisper, or Deepgram Nova-3 streaming.
- **Question audio:** Edge TTS, or Deepgram Aura-2.
- **Persistence:** PostgreSQL through SQLAlchemy and Flask-Migrate; local
  filesystem storage is available when no database is configured.
- **Resume parsing:** `pypdf` for PDFs containing extractable text. Scanned PDFs
  without text are not supported by OCR.

For Deepgram streaming, browser microphone audio goes through an authenticated
Flask WebSocket relay to Deepgram; the API key stays on the server. Gemini and
Deepgram calls require internet access and may incur provider usage or charges.

## Local setup

Use Python 3.14, Git, and a modern browser with microphone access. From the
project directory:

```sh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

On Windows, activate the environment with `venv\Scripts\activate` instead. Edit
`.env` and set your own `GEMINI_API_KEY`; do not commit or share it. The default
local providers are `STT_PROVIDER=whisper` and `TTS_PROVIDER=edge`. Whisper runs
on the local CPU and may download its model the first time it is used. Edge TTS
requires internet access.

For Deepgram, set `STT_PROVIDER=deepgram`, `TTS_PROVIDER=deepgram`, and a private
`DEEPGRAM_API_KEY` in `.env`. The defaults are Nova-3 and Aura-2 Thalia; model
settings and timeouts are in `.env.example`. Deepgram STT supports the automatic
voice flow. Manual mode still supports typing, but manual recorded-audio
transcription requires `STT_PROVIDER=whisper`.

Start the application:

```sh
python run.py
```

Open <http://127.0.0.1:5000>, enter a name and job description, and upload a PDF
resume. Review the instructions and microphone check, then start the interview
or choose manual mode. Browser microphone and camera access require permission;
remote deployments require HTTPS.

## Storage and security

With `DATABASE_URL` and a private, stable `SECRET_KEY`, manual and voice interview
state, questions, answers, follow-ups, evaluations, transcripts, scorecards, and
server-side sessions use PostgreSQL. See the
[PostgreSQL guide](docs/postgresql-integration.md) for ownership, session expiry,
recovery, and migration details. Lost or expired browser sessions do not have an
account-based recovery flow. Existing file-backed data is not automatically
imported into PostgreSQL; the importer is not implemented.

Without `DATABASE_URL`, the app uses local filesystem-backed sessions and voice
journals under `instance/`. This option is for local use or a single host with
appropriate shared storage; it is not a substitute for shared database storage
across deployment hosts.

Never commit `.env`, API keys, database passwords, or generated interview data.
Use HTTPS, secure cookies, a strong `SECRET_KEY`, and provider spending limits for
hosted use. Interview access uses session-bound capabilities; it is not a full
candidate account or authentication system. Test rate limits behind your proxy
before public use. Sessions, recordings, and provider data are subject to the
configured services and retention policies.

## Database migrations

Create or select the target PostgreSQL database, configure `DATABASE_URL` and
`SECRET_KEY` privately, and review the migration scripts before applying them.
For an existing database, back up your data first. From an activated environment:

```sh
flask --app app.database:migration_app db current
flask --app app.database:migration_app db upgrade
```

Run migrations before starting a fresh hosted service; the session interface can
otherwise create a table that conflicts with its migration. Do not run database
reset or downgrade commands on valuable data. The application does not migrate
old file journals automatically.

## Render deployment

The lightweight `requirements-production.txt` omits faster-whisper, PyAV,
NumPy, and Edge TTS. `requirements.txt` retains those packages for local use.
For a Render deployment using Deepgram and PostgreSQL:

```text
Build: pip install -r requirements-production.txt
Start: gunicorn -c gunicorn.conf.py run:app
```

Set `APP_ENV=production`, `DATABASE_URL`, `SECRET_KEY`, `GEMINI_API_KEY`,
`DEEPGRAM_API_KEY`, `STT_PROVIDER=deepgram`, `TTS_PROVIDER=deepgram`, and
`SESSION_COOKIE_SECURE=1` in the host's environment. Render supplies `PORT`.
The default Gunicorn configuration uses one threaded worker. See the
[Render guide](docs/render-free.md) for migration order, limits, and deployment
checks. A real deployment, live provider behavior, and peak memory under load must
be validated for the intended environment; a local mock test alone does not prove
that a 512 MB instance can support a particular workload.

## Testing

Run the automated Python and JavaScript suites without using live Gemini,
Deepgram, or TTS credentials:

```sh
python -m unittest discover -s tests -v
node --test tests/*.test.cjs
```

PostgreSQL integration tests require a **dedicated, migrated test database** and
`TEST_DATABASE_URL`; endpoint tests also require the application to start with
`DATABASE_URL` pointing to that test database. Run those tests separately from
the filesystem-session tests. Never point test settings at a database containing
valuable data. See the [PostgreSQL guide](docs/postgresql-integration.md) and
individual test files for setup.

For a local demo with mocked interview decisions:

```sh
PYTHONPATH=. python tests/voice_demo.py
```

Open <http://127.0.0.1:5001/voice-demo>. By default, STT and TTS are real and can
use network services or download models. The root-level `test_gemini.py`,
`test_stt.py`, and `test_tts.py` are manual service scripts and can consume API
quota or require local audio. `tests/browser_deepgram.cjs` is a Playwright browser
fixture intended for a separately configured mocked test server.

## Project structure

```text
run.py                          Flask application, routes, session setup
app/services/                   Gemini, speech, interview flow, persistence
app/routes/                     Conversation API and Deepgram WebSocket relay
app/models/                     SQLAlchemy models
app/static/                     Browser voice, camera, and UI assets
app/templates/                  Setup, interview, and scorecard pages
app/voice_config.py             Voice thresholds and limits
migrations/                     Flask-Migrate database revisions
tests/                          Mocked and PostgreSQL/browser integration tests
docs/                           Voice, PostgreSQL, and Render guides
requirements.txt                Full local dependencies
requirements-production.txt     Lightweight Deepgram deployment dependencies
```

## Current limitations and planned work

- Voice recognition can miss words, misinterpret commands, or react to background
  noise. If streaming fails, retry and repeat any words that were not finalized.
- The system waits for turn completion, Gemini evaluation, and question audio;
  it is not yet a low-latency, full-duplex voice conversation. Actual timing and
  recovery need testing with real devices, providers, and the deployment network.
- Background processing is not a durable job queue. Interrupted provider calls
  require explicit recovery, and retries can result in another provider request.
- **Planned: evaluate LiveKit** for a future real-time voice architecture,
  including streaming audio and interruption handling. LiveKit is not currently
  integrated; the architecture, deployment, costs, and migration plan are still
  to be decided.
- Further work includes candidate authentication and session recovery, robust
  usage limits, broader browser/device testing, and production monitoring.

Some older historical material in [the voice guide](docs/realtime-voice.md)
describes earlier branch behavior; use the provider and deployment sections above
for the current `feature/free-deployment` configuration.
