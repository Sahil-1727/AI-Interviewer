# LiveKit conversational interview

When `LIVEKIT_ENABLED=1`, the interview page uses LiveKit WebRTC as its only
voice transport. The browser does not connect to Deepgram, call the recorded
transcription endpoint, or fetch full-file TTS during a LiveKit interview.
Manual typed answers remain available. With LiveKit disabled, the existing
Deepgram/Whisper browser flow remains available.

## Runtime architecture

- Flask authenticates the browser session, creates the interview plan, checks
  interview ownership, and issues a five-minute room token. A short-lived signed
  ticket plus a shared worker secret authorizes the separate agent to fetch the
  interview context and save transcript events. The resume and job description
  are never sent in browser room metadata.
- The LiveKit Python worker runs the conversational session with streaming
  AssemblyAI STT, Gemma 4 31B, and Fish Audio S2.1 Pro through LiveKit Inference.
  Turn detection and interruptions are handled in the persistent LiveKit
  session. The agent opens with a brief greeting, then asks the Flask-approved
  questions and can ask one contextual follow-up per main question.
- Finalized agent and candidate messages are sent asynchronously to Flask with
  event IDs. PostgreSQL stores the ordered transcript. On End Interview, Flask
  evaluates the saved answer pairs, caches the scorecard, and serves it through
  the existing scorecard route. Greeting and readiness small talk are excluded
  from scored answers.

Provider model IDs are verified against the LiveKit Voice AI quickstart and the
existing worker config: `assemblyai/universal-3-6-pro`,
`google/gemma-4-31b-it`, and `fishaudio/s2.1-pro`. LiveKit Inference usage may
consume LiveKit credits.

## Local browser test

Use a dedicated migrated PostgreSQL test database. `scripts/livekit_demo.py`
reads `demo_db_url` from `.env`, refuses to use the same database as
`DATABASE_URL`, and sets up separate Flask and agent processes. Do not apply
migrations or run this against the production database.

Terminal 1:

```sh
source venv/bin/activate
python scripts/livekit_demo.py flask
```

Terminal 2:

```sh
source venv/bin/activate
python scripts/livekit_demo.py agent
```

Open `http://127.0.0.1:5000`, create an interview, check microphone access,
acknowledge the instructions, and start. Verify the greeting, first question,
natural speech, interruption, repeat request, follow-up, pause/resume, browser
reconnect, manual typed fallback, End Interview, and final scorecard. In the
browser network panel, a LiveKit interview should use LiveKit WebRTC and should
not call `/api/voice/stream`, `/api/transcribe`, or `/api/synthesize`.

## Production configuration

Set these on the Flask web service and separate agent worker, never in Git:

| Variable | Flask web | Agent worker |
| --- | --- | --- |
| `LIVEKIT_ENABLED=1` | Required to activate LiveKit UI/session endpoint | Not needed |
| `LIVEKIT_URL` | Required | Required |
| `LIVEKIT_API_KEY` | Required | Injected by LiveKit Cloud |
| `LIVEKIT_API_SECRET` | Required | Injected by LiveKit Cloud |
| `LIVEKIT_AGENT_SHARED_SECRET` | Random secret, at least 32 characters | Same secret |
| `LIVEKIT_AGENT_NAME=ai-interviewer` | Must match registered agent | Must match token dispatch |
| `LIVEKIT_AGENT_MODE=conversational` | Optional; conversational is default | Optional |
| `INTERVIEW_BACKEND_URL` | Not needed | HTTPS URL of the Flask service |
| `DATABASE_URL` | PostgreSQL URL with TLS | Not needed |
| `SECRET_KEY` | Stable private Flask session key | Not needed |
| `GEMINI_API_KEY` | Required for evaluation and scorecards | Not needed |
| `APP_ENV=production` | Production settings | Not needed |
| `SESSION_COOKIE_SECURE=1` | Required on HTTPS | Not needed |

Render web-service commands remain:

```text
Build: pip install -r requirements-production.txt
Start: gunicorn -c gunicorn.conf.py run:app
```

The Flask service can be deployed from Render. The root `Dockerfile` is for the
separate LiveKit Cloud worker; the existing Render Python service continues to
use the build and start commands above. LiveKit Cloud injects its own LiveKit
URL and credentials into the worker. Add a private secrets file outside the
repository containing `INTERVIEW_BACKEND_URL` (the HTTPS Render URL) and the
same `LIVEKIT_AGENT_SHARED_SECRET` used by Flask, then run:

```sh
lk cloud auth
lk agent create --secrets-file /path/to/private/livekit-worker.env
```

Choose a deployment region interactively. For later agent code updates, run
`lk agent deploy` from the repository root. Configure `LIVEKIT_ENABLED=1`, the
LiveKit project credentials, and the same worker secret in Render. The
worker-to-Flask URL must use HTTPS in production.
Apply the existing Alembic migrations to the intended database after backup and
revision review; application startup does not perform destructive migrations.

The old Deepgram and Whisper code remains for fallback/local development. Do
not configure a production app with `LIVEKIT_ENABLED=1` until its worker is
registered with the same LiveKit project and the real-audio flow has been
verified. Application interview limits, if configured, are not guaranteed
provider billing caps; configure usage alerts and billing safeguards in the
LiveKit dashboard.

## Current limitations

- Automated checks cannot verify real microphone, browser, LiveKit Cloud, model
  access, or provider credit behavior. A real-audio interview is still required.
- Evaluation is intentionally performed when the candidate ends the interview,
  so conversational turns do not wait for Gemini scoring. The scorecard may
  take longer than ordinary voice replies.
- A browser disconnect is recoverable by reconnecting, which creates a new
  room generation and rejects events from an older worker. Test partial turns
  and network loss with real audio before relying on recovery in production.
- Render Free can spin down and restart; use PostgreSQL-backed sessions and
  interview persistence, and expect the browser to reconnect after a web
  service restart.
