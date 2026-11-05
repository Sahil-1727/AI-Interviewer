# LiveKit deployment branch — experimental

This branch is a work in progress. **Do not deploy it for public use yet.** The
application has not passed a real-audio end-to-end interview or a production
security review. No hosted latency, interruption reliability, or memory target
has been measured. Changes are local and have not been pushed or deployed.

## Architecture

The existing Flask application remains authoritative for interview plans,
Gemini evaluation, follow-ups, scorecards, and PostgreSQL persistence. Browser
audio travels through LiveKit WebRTC to a separate agent process. The agent
uses LiveKit Inference with the same model identifiers and voice as the local
standalone demo:

- AssemblyAI `assemblyai/universal-3-6-pro` for streaming STT.
- Google `google/gemma-4-31b-it` for interpreting standalone repeat requests;
  it does not choose questions or score answers. The original answer text is
  sent to Flask, and a model error falls back to preserving that text.
- Fish Audio `fishaudio/s2.1-pro`, voice
  `fa4c9eb3dccc4806b382b40d61c6b10a`, for streaming TTS.

These Inference services may consume LiveKit credits. They are only used when
an interview session runs. See [AssemblyAI integration](https://docs.livekit.io/agents/models/stt/assemblyai/),
[LiveKit Inference](https://docs.livekit.io/agents/models/inference/), and
[LiveKit pricing](https://livekit.io/pricing) for current availability and rates.
No additional provider account is created by this code.

## Restricted access and ownership

Production requires `LIVEKIT_ENABLED=1`, PostgreSQL-backed sessions, a stable
private Flask `SECRET_KEY`, secure cookies, and `RESTRICTED_ACCOUNTS_JSON`.
The latter is an administrator-maintained JSON object mapping usernames to
Werkzeug password hashes. There is no public registration. To generate a hash
locally without putting the password in shell history:

```sh
python -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass("Password: ")))'
```

Store the resulting hash in the hosting provider's environment settings, for
example as `{"candidate":"<generated-hash>"}`. Do not commit it or print it in
shared logs. Updating accounts requires updating configuration and restarting
workers. Interview access remains protected by the existing hashed interview
capability and the account recorded in the interview snapshot. Old interviews
without an account owner are not automatically reassigned.

Login POSTs use CSRF validation and a PostgreSQL-backed per-client rate limit
(default five per minute). Review trusted proxy configuration before public use;
the current limiter sees the connection's remote address and can group users
behind a proxy. Logout clears the session. Expired/lost sessions cannot yet be
recovered through an account dashboard.

## Usage limits

The `interview_usage` table and a PostgreSQL transaction-level advisory lock
serialize admission across workers. One account can have one active interview.
Limits are controlled by `INTERVIEW_MAX_DURATION_SECONDS` (default 900),
`INTERVIEW_MAX_PER_USER_PER_DAY` (3), `INTERVIEW_MAX_GLOBAL_PER_DAY` (20), and
`INTERVIEW_MAX_ACTIVE` (5). Days use the database's UTC timestamp. Reconnection
to the same active interview keeps its original deadline and does not consume a
new daily admission. Expired records are closed during admission and heartbeat;
`flask --app run cleanup-expired-interviews` can close expired usage records
without deleting interviews.

The worker polls Flask and shuts down if the interview ends or authorization
fails; the browser also attempts to disconnect at the deadline. This is **not a
guaranteed provider billing cap**: a crash, network partition, already-issued
room token, or delayed shutdown can still consume LiveKit/WebRTC or provider
resources. Configure usage alerts, billing limits, and account safeguards in the
LiveKit Cloud dashboard where available, and review its current plan terms.
These safeguards require an administrator and are not configured automatically.

## Flask and database setup

Flask requires `APP_ENV=production`, `DATABASE_URL`, `SECRET_KEY`,
`SESSION_COOKIE_SECURE=1`, `GEMINI_API_KEY`, `LIVEKIT_ENABLED=1`, `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_AGENT_SHARED_SECRET` (at least
32 random characters), and `RESTRICTED_ACCOUNTS_JSON`. Optional limit settings
are in `.env.example`. In production the old Deepgram/Whisper voice routes are
unavailable; typed manual interviewing is retained. The older code remains in
the repository for local development.

Before starting a new environment, back up existing data, inspect the Alembic
revisions, and run these commands only against the intended database:

```sh
flask --app app.database:migration_app db current
flask --app app.database:migration_app db upgrade
```

Revision `e1c9a02a4141` adds usage records and a partial unique index. It has
been tested on an isolated local PostgreSQL database, not applied to production.
No existing file data is imported automatically.

The Render Flask commands are:

```text
Build: pip install -r requirements-production.txt
Start: gunicorn -c gunicorn.conf.py run:app
```

`/health/ready` checks database connectivity. No service has been deployed.

## Separate worker

Install and run separately from Flask. The worker requires `LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_AGENT_SHARED_SECRET` (the same
value as Flask), and `INTERVIEW_BACKEND_URL`. Use HTTPS for the backend except
`http://127.0.0.1:<port>` during local testing. The Gemini API key stays in Flask.

```sh
pip install -r requirements-livekit-agent.txt
python livekit_agent/interview_agent.py dev
```

`Dockerfile.livekit` is an initial separate worker image. Docker is unavailable
in the current test environment, so this command has not been verified:

```sh
docker build -f Dockerfile.livekit -t ai-interviewer-livekit .
```

LiveKit Cloud's [deployment guide](https://docs.livekit.io/deploy/agents/quickstart/)
describes `lk agent create` and `lk agent deploy`. Set worker secrets separately
and ensure the deployment uses the worker Dockerfile. Neither command was run;
Flask deployment does not automatically deploy the worker.

## Isolated real-audio test

Use a dedicated test database and test credentials, not production data. Real
LiveKit Inference and Gemini requests may use credits. Configure `demo_db_url`,
LiveKit Cloud URL/key/secret, the shared agent secret, and Gemini key privately
in `.env`; apply the migrations only to the dedicated test database. The local
launcher refuses a demo URL matching `DATABASE_URL` and starts separate
processes without printing secrets:

```sh
source venv/bin/activate
python scripts/livekit_demo.py flask
```

In a second terminal:

```sh
source venv/bin/activate
python scripts/livekit_demo.py agent
```

Open `http://127.0.0.1:5000`, create an interview, check the microphone, and
select **Start LiveKit interview (experimental)**. Test an answer, an
interruption, repeat, Done Speaking, pause/resume, a follow-up, disconnect and
reconnect, End Interview, and the scorecard. Verify each answer is evaluated
once and belongs to the right question. For restricted-login testing, configure
`RESTRICTED_ACCESS=1` and `RESTRICTED_ACCOUNTS_JSON` for the local processes.
The launcher creates a new Flask session secret on restart.

## Remaining verification and limitations

- Complete real browser-to-agent audio and Cloud deployment tests have not run.
  Model intent classification may misinterpret a repeat request; it needs testing
  on natural speech. Provider/model support is verified from documentation and
  demo source, not through a new live API call.
- Browser reconnect creates a new room generation and rejects superseded agents,
  but cross-worker behavior, stale room cleanup, token expiry, partial audio,
  and account revocation during a network partition need live testing.
- Interview duration and daily limits are application controls, not guaranteed
  spending caps. The current design lacks independent server-side forced room
  deletion during a worker crash or prolonged network partition.
- Prompt-injection resistance, login throttling behind the target proxy, password
  management, transcript privacy, and concurrent load require security review.
- Worker logs include timing fields for STT, Gemini evaluation, and TTS. Browser
  active-speaker timing is an estimate, not an exact first-audible measurement.
  No CPU, memory, latency or live cost measurements are available.
- The worker image has not been built or smoke-tested because Docker is absent.
  The full cloud end-to-end test and production database migration require
  explicit manual configuration; no deployment is performed automatically.
