# Scaling and request capacity

The Flask service can use multiple Gunicorn workers or multiple hosts sharing
PostgreSQL and the same SECRET_KEY. Sessions, ownership, rate buckets and interview
claims are shared through PostgreSQL. Use identical limit settings on every host.
Keep the LiveKit worker separate from Flask; its concurrency and provider quotas
must be sized independently.

## Start small on Render Free

Use `gunicorn -c gunicorn.conf.py run:app` with:

```dotenv
GUNICORN_WORKERS=1
GUNICORN_THREADS=4
HTTP_PROVIDER_CONCURRENCY=2
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
DB_POOL_TIMEOUT_SECONDS=5
DB_POOL_RECYCLE_SECONDS=300
```

HTTP_PROVIDER_CONCURRENCY limits simultaneous synchronous provider requests per
process (setup/plan generation, manual answers, audio and LiveKit session issuance).
Excess requests receive 503 with Retry-After: 2 instead of waiting in another
application queue. Capacity is released on success and exceptions. It does not
limit active WebRTC rooms or background evaluation jobs. Control/end requests and
health checks do not acquire these slots. Network/server queues can still exist.

Background evaluation/finalization already uses CONVERSATION_WORKERS and
CONVERSATION_QUEUE_LIMIT per process. Keep the queue bounded and configure provider
timeouts. LIVEKIT_FINALIZE_EVALUATION_CONCURRENCY can increase provider concurrency
within each finalization job; include that multiplier in your provider budget.
Neither these queues nor the HTTP admission semaphore are a durable distributed
job scheduler. Interrupted jobs use the existing stale/retry mechanism.

## Shared request budgets

All limits below are fixed windows of one minute and use atomic PostgreSQL
UPSERTs. They survive restarts and apply across workers and hosts. Rejected
requests receive 429 and Retry-After until the next minute. IP and authenticated
account budgets apply together, so changing IP does not bypass an account budget.
Fixed windows can allow a burst on either side of a minute boundary.

| Environment variable | Default | Scope |
| --- | ---: | --- |
| RATE_LIMIT_AUTH_PER_MINUTE | 12 | Login/signup per IP |
| RATE_LIMIT_AUDIO_PER_MINUTE | 30 | Audio requests per IP and signed-in account |
| RATE_LIMIT_INTERVIEW_PER_MINUTE | 60 | Other browser mutations per IP and account |
| RATE_LIMIT_READ_PER_MINUTE | 120 | Status polling and interview history per IP and account |
| RATE_LIMIT_WORKER_PER_MINUTE | 600 | Agent callbacks per source IP |
| RATE_LIMIT_PROVIDER_GLOBAL_PER_MINUTE | 30 | Expensive synchronous POSTs across the whole deployment |

The global provider budget is a request rate, not a concurrent-room limit, token
budget, billing cap or limit on background evaluations. Agent endpoints retain
their existing shared-secret and signed-ticket authentication; their rate budget
does not grant access. Database failures deny rate-limited requests with 503.

Configure TRUSTED_PROXY_HOPS for the actual trusted reverse proxy chain so remote
addresses are meaningful. It defaults to 0 (ignore forwarded headers). Behind one
trusted proxy, use 1 only after confirming incoming forwarding headers are sanitized
and direct access to the backend is blocked. An incorrect value can group all users
under the proxy IP or allow spoofing. Users sharing an IP also share IP budgets.

Run the existing maintenance command periodically to remove expired session and
rate-limit rows (completed interviews are retained):

```sh
flask --app run:app cleanup-expired-sessions
```

No new migration is required; these limits use the existing request_limits table.
Apply existing migrations before starting a fresh database:

```sh
flask --app app.database:migration_app db upgrade
```

## Scale after measurement

On a larger host, increase GUNICORN_WORKERS and threads after load tests. Total
HTTP provider slots are workers × hosts × HTTP_PROVIDER_CONCURRENCY. Maximum
configured pooled DB connections are workers × hosts × (DB_POOL_SIZE +
DB_MAX_OVERFLOW), plus LiveKit worker/admin connections. Leave headroom for session
writes and background jobs; a pool timeout returns an unavailable response rather
than opening unlimited connections. Use Neon's pooled URL and TLS in production.
Do not assume more workers fit within Render Free's 512 MB.

Monitor request latency, 429/503 rates, DB pool waits, memory, background queue
saturation, LiveKit active sessions, provider quotas and voice response latency.
Flask scaling alone cannot speed up model inference or remove LiveKit credit caps.
GUNICORN_MAX_REQUESTS and GUNICORN_MAX_REQUESTS_JITTER are optional, disabled by
default; worker recycling requires testing long WebSockets and shutdown draining.
Actual voice latency and production capacity require a real-audio/load test.

## Verification

Run `python -m unittest discover -s tests -p test_capacity_limits.py` for shared
account/global buckets, simultaneous SQL consumers, overload and exception cleanup.
Set TEST_DATABASE_URL to a dedicated migrated PostgreSQL database to also run the
independent-connection-pool concurrency test. Existing test_gunicorn_sessions.py
checks simultaneous session updates in two real Gunicorn processes. Never point
the integration suite at a production database. Provider services must be mocked.
