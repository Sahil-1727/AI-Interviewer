# PostgreSQL integration (in progress)

When `DATABASE_URL` is configured, new manual and voice interviews use PostgreSQL
for the conversation journal, normalized records, and Flask sessions. Without
it, the existing file-backed behavior remains. Existing file journals and sessions
are not automatically imported; back them up before migration.

For an isolated local development database, create an empty PostgreSQL database
using your existing PostgreSQL administration tools, then set `DATABASE_URL` to
its connection URL, for example `postgresql+psycopg://USER:PASSWORD@localhost:5432/ai_interviewer`.
Keep credentials outside source control. Managed providers may require the
appropriate `sslmode` URL parameter and certificate configuration.

Set a strong, private `SECRET_KEY` shared by every worker; the app refuses to
start with PostgreSQL configured when it is missing. With the virtual environment
active, install `requirements.txt`, then run:

```sh
flask --app app.database:migration_app db upgrade
```

The command applies schema migrations; it does not import interviews. Restart the
application with `DATABASE_URL` set to enable PostgreSQL for new interviews.
Without it the existing file journal remains active. Back up an existing database before
running migrations. Do not run a downgrade against valuable data: the generated
downgrade drops tables. Do not use production credentials for tests.

The browser session contains a random server-side session identifier; PostgreSQL
stores the interview access token in the associated session record. Expired or
lost sessions cannot be recovered without authentication or a separate recovery
mechanism. Workers must share the database and the same secret key. An external
Gemini call can have an ambiguous outcome after a crash: retry is explicit and
can cause a second provider call, though committed state is protected.

Voice endpoints authorize the interview against the hashed access capability.
They write messages with the journal transition, under the interview row lock.
Existing file interviews do not receive capabilities merely by applying migrations.

`GET /api/conversation/transcript` returns the current session's manual or voice
transcript after verifying its access token; it is not accessible by ID alone.
The response is marked `no-store`.
Browser POST forms include a CSRF field; same-origin JavaScript POSTs use the
`X-CSRF-Token` header. Other clients using state-changing endpoints must also
obtain the token from the page and retain its session cookie.

Before deployment, configure `APP_ENV=production`, a strong `SECRET_KEY` of at
least 32 characters shared by all workers, HTTPS, `SESSION_COOKIE_SECURE=1`, and
database TLS as required by the provider. Production mode refuses disabled CSRF
or rate limiting and debug mode. Configure `SESSION_RETENTION_SECONDS` (default
86400); schedule `flask --app run cleanup-expired-sessions` to remove expired
browser sessions and rate counters older than 24 hours. It never deletes
interviews. CSRF tokens protect browser POSTs. Shared PostgreSQL rate counters
default to 60 interview and 30 audio requests per IP per minute; tune
`RATE_LIMIT_INTERVIEW_PER_MINUTE` and `RATE_LIMIT_AUDIO_PER_MINUTE` for traffic.
Limits use the connection's remote address and do not trust forwarded headers:
behind a proxy they may apply to all users together. Configure trusted proxy
handling and network restrictions at the hosting layer before deployment.
Independent concurrent session changes merge under a database row lock; divergent
changes to the same key return a 409 conflict so the user can refresh. More complex
concurrent mode switches still require production-load validation.
The existing background thread executor is per worker, not a durable queue;
interrupted provider calls become recoverable after the configured stale timeout.
Gunicorn's graceful timeout bounds worker shutdown; requests or background calls
still in progress at termination may become recoverable only after that timeout
and `CONVERSATION_STALE_SECONDS`. Avoid claiming exactly-once provider calls.
`GET /health/ready` checks database connectivity without returning credentials.
The historical data importer is not implemented.
Size the host for CPU/RAM-intensive faster-whisper and concurrency; do not assume
a free tier can handle it. A production command after migration is, for example,
`gunicorn -c gunicorn.conf.py run:app`.

For tests, use a dedicated migrated PostgreSQL database and a test-only secret;
the integration tests create records and do not call Gemini:

```sh
export SECRET_KEY='a-test-only-secret'
export CSRF_ENABLED=0 RATE_LIMIT_ENABLED=0 # Existing flow regression tests only.
export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/interviewer_test'
export TEST_DATABASE_URL="$DATABASE_URL"
flask --app app.database:migration_app db upgrade
python -m unittest discover -s tests -p test_postgres_voice_endpoints.py -v
python -m unittest discover -s tests -p test_gunicorn_sessions.py -v
```

Never disable CSRF or rate limiting in production. `tests/test_request_security.py`
enables CSRF explicitly to test missing and valid tokens. The manual form waits
for evaluation within the web request, so configure WSGI and proxy timeouts
accordingly. Browser cookies expire when the browser session ends; server-side
records expire after the configured retention period. The cleanup command requires
database permissions to delete only session and request-limit records.
