# Render Free deployment

This configuration supports browser microphone streaming with Deepgram STT and
TTS, Gemini, and Neon PostgreSQL. For the persistent LiveKit conversational
experience, set `LIVEKIT_ENABLED=1` and deploy the separate agent described in
[`livekit-integration.md`](livekit-integration.md); that disables the legacy
Deepgram voice endpoints in production while keeping typed manual answers.
Local development can continue to install `requirements.txt` and use Whisper
and Edge TTS. Render uses `requirements-production.txt`; it does not download
Whisper models.

## Prepare the database first

Use a dedicated Neon database and a TLS-protected connection string. Back up any
existing data, inspect the migration scripts under `migrations/versions`, and
check the current revision before upgrading. Do not stamp or reset an existing
database without understanding its history. Run the following on a trusted local
machine with the production requirements installed, substituting your private
Neon URL without committing it:

```sh
export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require'
flask --app app.database:migration_app db current
flask --app app.database:migration_app db upgrade
```

Run migrations **before starting the web service**. The Flask session interface
can create its session table during application startup, which would otherwise
conflict with the corresponding migration on a fresh database. Upgrades change
the schema; review and back up first. Never run downgrade, reset, or drop commands
as part of deployment. Neon connectivity and existing data must be checked by
the operator; only a fresh local PostgreSQL database has been tested here.

## Create the Render web service manually

Select the `feature/free-deployment` branch, Python runtime, and Free instance.
Use these exact commands:

Build command:

```sh
pip install -r requirements-production.txt
```

Start command:

```sh
gunicorn -c gunicorn.conf.py run:app
```

Set these environment variables in Render's environment settings, not in Git:

| Name | Value |
| --- | --- |
| `APP_ENV` | `production` |
| `DATABASE_URL` | Private Neon PostgreSQL connection URL, with `sslmode=require` |
| `SECRET_KEY` | Private, random value of at least 32 characters; keep stable across restarts |
| `GEMINI_API_KEY` | Private Gemini API key |
| `DEEPGRAM_API_KEY` | Private Deepgram key; required only for Deepgram voice mode |
| `STT_PROVIDER` | `deepgram` for Deepgram mode; LiveKit mode uses the separate agent |
| `TTS_PROVIDER` | `deepgram` for Deepgram mode; LiveKit mode uses the separate agent |
| `SESSION_COOKIE_SECURE` | `1` |

Render supplies `PORT`; do not override `GUNICORN_BIND`. The default is one
Gunicorn `gthread` worker with four threads and at most two simultaneous
Deepgram streams per worker. Additional tuning and model settings are listed
in `.env.example`; `GUNICORN_GRACEFUL_TIMEOUT_SECONDS=25` is recommended on
Render because its shutdown window is limited. `/health/ready` checks database
connectivity and the presence of the tables required by this application. It
does not verify the exact Alembic revision or external provider availability;
apply migrations before deploying and test Gemini/Deepgram separately.

The database and keys are required; do not put them in build commands, logs, or
source control. Rotating `SECRET_KEY` invalidates existing browser sessions.
After applying the account migration, create the first super-admin through the
private Render shell with `flask --app run create-super-admin admin@example.com`.
The command prompts for the password. Candidates can then self-register; the
admin console is not linked from candidate pages. See
[`accounts-and-quotas.md`](accounts-and-quotas.md) for account limits and access.

## Limitations before a public launch

- A successful local mock test does not establish that live Deepgram, Gemini,
  Neon, or Render works. Test a real interview and migration against the intended
  services, and measure peak memory under concurrent traffic on the 512 MB plan.
- Render Free can spin down and restart; WebSockets disconnect and in-flight
  processing may be interrupted. Review recovery behavior and test reconnection.
  Its filesystem is ephemeral, so PostgreSQL is required for persistent state.
- Rate limiting currently uses the request's remote address. Verify trusted proxy
  configuration on Render before public use; a shared proxy address can cause
  unrelated candidates to share limits. Do not trust forwarded headers blindly.
- Set provider spending limits and monitor usage before opening the service to
  the public. Existing request limits are not a per-candidate spending quota.
- Deepgram mode supports the streaming voice flow; local Whisper remains available
  through the full requirements. See `docs/realtime-voice.md` for manual-mode
  audio limitations when using Deepgram.

See [Render Free limits](https://render.com/docs/free),
[WebSocket behavior](https://render.com/docs/websocket), and
[web service port binding](https://render.com/docs/web-services).
