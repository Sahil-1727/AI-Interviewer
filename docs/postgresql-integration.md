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

Before deployment, configure a strong `SECRET_KEY` shared by all workers, HTTPS,
`SESSION_COOKIE_SECURE=1`, database TLS as required by the provider, CSRF
protection, rate limiting, and a session-retention policy. Flask-Session's SQL
backend can overwrite concurrent updates to the same browser session; mode
switches and parallel requests need further multi-process concurrency validation.
The existing background thread executor is per worker, not a durable queue;
interrupted provider calls become recoverable after the configured stale timeout.
The database-aware health check and historical data importer are not implemented.
Size the host for CPU/RAM-intensive faster-whisper and concurrency; do not assume
a free tier can handle it. A production command after migration is, for example,
`gunicorn --workers 2 --threads 4 --bind 0.0.0.0:8000 run:app`.

For tests, use a dedicated migrated PostgreSQL database and a test-only secret;
the integration tests create records and do not call Gemini:

```sh
export SECRET_KEY='a-test-only-secret'
export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/interviewer_test'
export TEST_DATABASE_URL="$DATABASE_URL"
flask --app app.database:migration_app db upgrade
python -m unittest discover -s tests -p test_postgres_voice_endpoints.py -v
```

The manual form waits for evaluation within the web request, so configure WSGI
and proxy timeouts accordingly. Browser session cookies expire when the browser
session ends; server-side records use Flask's configured permanent session
lifetime. Implement a retention and expired-session cleanup policy for production.
