# Accounts, interview quotas, and account administration

Public signup creates a normal member account. Passwords are stored as
Werkzeug password hashes; the browser session stores only the account ID.
Accounts start with a limit of two interview creations per UTC day. Interview
creation reserves quota in the same PostgreSQL transaction as the interview
record, serialized on the account row so simultaneous requests cannot exceed
the limit. The limit is per account and is not a provider billing guarantee.

## Apply the migration safely

Back up the intended database and review the migration before applying it. It
adds nullable email/password fields to existing user rows, role/active/quota
columns with safe defaults, and a daily usage table. It does not remove or
rewrite existing interview records. Do not point this command at a database
unless it is the intended deployment database.

```sh
flask --app run db upgrade
```

## Create the administrator account

Create the first administrator from a trusted local shell or the hosting
provider's private shell after applying the migration:

```sh
flask --app run create-super-admin admin@example.com
```

The command prompts for a password twice and never prints it. Use a unique
password and a private shell. The command refuses to create a second super
administrator. Public signup cannot request or assign admin privileges.

The private console is at `/internal/ops/accounts`. It is intentionally not
linked from candidate pages, returns 404 for non-admin users, and requires a
logged-in administrator session plus CSRF protection. The URL is not an
authentication boundary: server-side role checks are. The page lists account
IDs, emails, signup dates, today/all-time interview counts, and lets the admin
set an individual daily limit from 0 to 100 and deactivate an account. A limit
of zero blocks new interview creation without deleting existing data.

## Candidate flow

1. A candidate signs up with name, email, and a password of at least 12
   characters, then remains signed in.
2. On later visits they sign in with email and password. There is no public
   admin sign-up or role selection.
3. The setup page shows daily usage. Interview creation is rejected once the
   account reaches its current limit.
4. Logging out clears the server-side session. Deactivated accounts are signed
   out on their next request; their interview records remain intact.

Signup and login share the configured auth request rate limit. Keep CSRF,
PostgreSQL-backed sessions, HTTPS, secure cookies, and existing application
rate limits enabled in production. Self-service password recovery is not
implemented yet; do not promise email recovery until a verified reset flow is
added.
