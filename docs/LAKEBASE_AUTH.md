# Lakebase authentication: stopping the hourly expiry

Databricks Lakebase offers two ways to authenticate, and for a scheduled
pipeline the difference is the whole ballgame.

| | Native Postgres password role | OAuth token |
|---|---|---|
| Lifetime | **Does not expire** | **60 minutes** |
| Tied to a Databricks identity | No | Yes |
| Works with a fixed `DATABASE_URL` secret | Yes | No — breaks within the hour |
| Code needed | None | Token minting and refresh |

**Use a password role.** Databricks' own documentation recommends it for
exactly this case: applications that cannot refresh credentials often.
Everything below is how to get one, plus what the pipeline does if you
cannot have one.

Two limits apply to connections either way, and neither matters here: a
24-hour idle timeout and a 3-day maximum connection life. Each ETL run
connects, works for a minute or two, and exits.

---

## Option A — password role (recommended)

### 1. Enable password connections

New Lakebase projects have password connections **disabled**, so this comes
first. In the Lakebase app: your project → **Settings** → turn on password
connections. If you cannot find it, it is under the project's
authentication or security settings; the Databricks docs page is "Manage
password connections".

### 2. Create or confirm a password role

Go to your branch → **Roles & Databases**.

- If the role (`etl_bot`) has a **Reset password** option, it is already a
  password role. Nothing to create.
- If it does not, it is an OAuth role and has no password to reset. Add a
  new role and choose **password role**, not OAuth role.

To set your own password instead of a generated one, from the SQL editor:

```sql
ALTER USER etl_bot WITH PASSWORD 'your-password';
```

Resetting from the UI generates a password with 60-bit entropy, which is
fine — just copy it immediately, because it is shown once.

### 3. Put the connection string in GitHub

Project → **Connect** → pick the branch, compute, database and the password
role → **Copy snippet**. Paste it straight into GitHub as the
`DATABASE_URL` secret. Never into a file, a chat, or a commit.

```
postgresql://etl_bot:PASSWORD@ep-xxxx.database.us-east-2.cloud.databricks.com/marketdb?sslmode=require
```

That is it. The password does not expire, so nothing else is needed and the
pipeline uses the string exactly as given.

### 4. Protect the branch

The branch in this project was archived automatically for inactivity, which
is also why its compute disappeared. On the branch overview there is a
**Protect** button next to "Enable branch protection to prevent this branch
from being archived". Turn it on.

Once the ETL runs every 30 minutes the branch is never idle long enough to
be archived, but it needs to survive until then.

---

## Option B — OAuth with automatic refresh

Use this only if password connections are blocked for your workspace.

Set four more secrets and the pipeline mints a fresh credential for every
connection and refreshes it mid-run:

| Secret | Value |
|---|---|
| `DATABRICKS_HOST` | `https://<workspace>.cloud.databricks.com` |
| `DATABRICKS_CLIENT_ID` | the service principal's client ID (a UUID) |
| `DATABRICKS_CLIENT_SECRET` | its OAuth secret |
| `LAKEBASE_ENDPOINT` | `projects/<id>/branches/<id>/endpoints/<id>` |

All four must be present; with any of them missing the pipeline uses
`DATABASE_URL` unchanged. `DATABASE_URL` is still required — it supplies
the host, database and query string, and only its password is swapped for
the token.

### Setting it up

1. Create a service principal in the workspace and give it an OAuth secret
   (Settings → Identity and access → Service principals).
2. Give it a Postgres role on the branch. From the SQL editor:
   ```sql
   SELECT databricks_create_role('<client-id>', 'SERVICE_PRINCIPAL');
   GRANT CONNECT ON DATABASE marketdb TO "<client-id>";
   GRANT USAGE, CREATE ON SCHEMA public TO "<client-id>";
   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "<client-id>";
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
     GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "<client-id>";
   ```
   The `ALTER DEFAULT PRIVILEGES` line matters: without it, tables created
   by a later migration are invisible to the service principal.
3. Find the endpoint path under the branch's compute details, or:
   ```bash
   databricks postgres list-endpoints projects/<project-id>/branches/production -o json
   ```

### How the refresh works

`pipeline/auth.py` does two calls, the same pair the Databricks docs
describe:

```
POST {host}/oidc/v1/token                  client id + secret -> workspace token
POST {host}/api/2.0/postgres/credentials   workspace token    -> database token
```

The database token is cached and reused until it comes within
`TOKEN_REFRESH_MARGIN` seconds of expiry (default 300). At that point
`db.get_connection()` closes the connection and opens a new one with a
fresh credential, so a backfill that runs longer than an hour keeps
working instead of failing halfway through.

Both tokens last 60 minutes, so the margin leaves 55 minutes of use per
credential — no meaningful extra API traffic.

### If something is wrong

The errors name the setting to check rather than surfacing a bare 401,
because these runs are unattended:

| Message mentions | Look at |
|---|---|
| `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` | the service principal's secret, or whether it expired |
| `LAKEBASE_ENDPOINT` | the endpoint path, and whether the principal has a Postgres role on that branch |
| could not reach the Databricks OIDC endpoint | `DATABRICKS_HOST`, including the `https://` |

---

## What the tests cover

`tests/test_auth.py`, with no calls to Databricks:

- OAuth engages only when all four settings are present, so a password role
  is never dragged through the token machinery
- a fresh token is served from cache rather than re-minted
- a token inside the refresh margin **is** re-minted
- a token lifetime is never trusted beyond an hour, and an unparseable
  expiry falls back to an hour
- swapping the password keeps the host, port, database, `sslmode` and any
  extra query parameters
- a token containing `/`, `:` or `@` round-trips through libpq's own URI
  parser instead of breaking the URL
- against the real test database: when the credential goes stale, the
  connection is replaced, queries keep working, the old connection is
  closed rather than leaked, and a fresh credential causes no reconnect
  churn

Each of those was mutation-tested: the behaviour was broken on purpose and
the suite confirmed failing.

---

## Sources

- [Connect with psql — Databricks](https://docs.databricks.com/aws/en/oltp/projects/connect-psql)
- [About authentication — Lakebase](https://learn.microsoft.com/en-us/azure/databricks/oltp/projects/authentication)
- [Manage roles — Lakebase](https://learn.microsoft.com/en-us/azure/databricks/oltp/projects/manage-roles)
- [Connect an external app using the API](https://docs.databricks.com/aws/en/oltp/projects/external-apps-manual-api)
