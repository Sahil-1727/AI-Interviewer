# PostgreSQL integration (in progress)

When `DATABASE_URL` is configured, new voice interviews use PostgreSQL for the
conversation journal and normalized interview records. Manual interviews and
Flask sessions continue to use filesystem storage. Do not deploy this branch
as a fully PostgreSQL-backed or horizontally scalable application. Existing
file journals are not automatically imported; back them up before migration.

For an isolated local development database, create an empty PostgreSQL database
using your existing PostgreSQL administration tools, then set `DATABASE_URL` to
its connection URL, for example `postgresql+psycopg://USER:PASSWORD@localhost:5432/ai_interviewer`.
Keep credentials outside source control. Managed providers may require the
appropriate `sslmode` URL parameter and certificate configuration.

With the virtual environment active, install `requirements.txt`, then run:

```sh
flask --app app.database:migration_app db upgrade
```

The command applies schema migrations; it does not import interviews. Restart the
application with `DATABASE_URL` set to enable PostgreSQL for new voice sessions.
Without it the existing file journal remains active. Back up an existing database before
running migrations. Do not run a downgrade against valuable data: the generated
downgrade drops tables. Do not use production credentials for tests.

Outstanding work includes replacing the manual/session stores, transcript export,
historical data import, health checks, deployment integration, and additional
production recovery and multi-host tests. A voice session's access token remains
in the filesystem-backed Flask session; loss of that session prevents resuming it.
Workers must share that session storage and the same secret key. An external
Gemini call can have an ambiguous outcome after a crash: retry is explicit and
can cause a second provider call, though committed state is protected.

Voice endpoints authorize the interview against the hashed access capability.
They write messages with the journal transition, under the interview row lock.
Existing file interviews do not receive capabilities merely by applying migrations.
