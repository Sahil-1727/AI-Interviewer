# PostgreSQL integration (in progress)

The schema and initial migration are a foundation only. The interview application
still uses filesystem sessions and its file-based conversation journal. Do not
deploy this branch as a PostgreSQL-backed or multi-worker-safe application yet.
No existing interview data has been migrated. Existing data must be backed up and
imported with an explicit, verified process before changing storage.

For an isolated local development database, create an empty PostgreSQL database
using your existing PostgreSQL administration tools, then set `DATABASE_URL` to
its connection URL, for example `postgresql+psycopg://USER:PASSWORD@localhost:5432/ai_interviewer`.
Keep credentials outside source control. Managed providers may require the
appropriate `sslmode` URL parameter and certificate configuration.

With the virtual environment active, install `requirements.txt`, then run:

```sh
flask --app app.database:migration_app db upgrade
```

The command applies the schema migration; it does not import interviews or switch
the running application to database storage. Back up an existing database before
running migrations. Do not run a downgrade against valuable data: the generated
downgrade drops tables. Do not use production credentials for tests.

Outstanding work includes replacing the session and conversation stores,
transactional and idempotent multi-worker processing, secure owner verification,
transcript export, data import, health checks, deployment integration, and
end-to-end PostgreSQL concurrency and recovery tests.
