# Running on Render Postgres (free plan)

The architecture is two free pieces:

```
GitHub Actions                 Render Postgres              Power BI
   the engine                    the storage                 the view
runs and exits                holds the tables            reads bi_* views
```

Render is the database only. GitHub Actions runs the ETL, because **Render's
free plan has no cron jobs or background workers** — only web services,
Postgres and Key Value. A free web service also spins down after 15 minutes
without traffic, which is the keep-alive problem this project deliberately
avoids.

---

## Read this before anything else

**A Render free Postgres database expires 30 days after it is created.**
After expiry there is a 14-day grace period to upgrade, and then Render
**deletes the database.** Not suspended, not paused — deleted, with the
data.

So two things are not optional:

1. **Know your expiry date.** Open the database in the Render dashboard; a
   free database shows it.
2. **Keep backups.** `.github/workflows/backup.yml` dumps the database
   every Sunday to a GitHub Actions artifact kept for 90 days, which covers
   the 30-day lifetime plus the grace period. An expiry then costs you a new
   database and one restore command, not your history.

Set `DB_EXPIRES_AT` as an Actions **variable** (not a secret) to that date
and the health monitor counts down: OK above 14 days, WARN at 14, FAIL at 7.

| Days left | Monitor says |
|---|---|
| 24 | `OK` — 24 day(s) left |
| 11 | `WARN` — 11 day(s) left |
| 2 | `FAIL` — 2 day(s) left |
| past | `FAIL` — expired N day(s) ago, restore from a backup now |

---

## Setup

### 1. Create the tables

Already done if you ran `market_pipeline_setup.sql` in pgAdmin. Otherwise
either run that file, or use **Actions → Database → migrate**, which applies
`sql/01_schema.sql` and `sql/02_bi_views.sql` from this repo.

Expect 11 tables and 16 `bi_*` views. The tables are empty until the ETL
runs; `dim_source`, `dim_interval` and `dim_indicator` arrive pre-seeded.

### 2. Get the connection string

Render dashboard → your Postgres → **Connections**. Use the **External
Database URL**, not the internal one: the internal hostname only resolves
from inside Render, and GitHub Actions is outside.

```
postgresql://USER:PASSWORD@dpg-xxxxx-a.frankfurt-postgres.render.com/DBNAME
```

Append `?sslmode=require`. Render requires SSL for external connections, and
being explicit beats relying on a client default.

### 3. Store it in GitHub

**Settings → Secrets and variables → Actions → Secrets tab → New repository
secret**, named exactly `DATABASE_URL`. The Secrets tab, not Variables, and
the name is case sensitive.

While you are there:

| Name | Kind | Value |
|---|---|---|
| `DATABASE_URL` | Secret | the external URL above |
| `FINNHUB_API_KEY` | Secret | |
| `ALPHAVANTAGE_API_KEY` | Secret | |
| `TWELVEDATA_API_KEY` | Secret | |
| `SYMBOLS` | Secret | optional, default `AAPL,MSFT,GOOGL` |
| `DB_EXPIRES_AT` | **Variable** | `YYYY-MM-DD` from the Render dashboard |
| `STORAGE_CAP_MB` | Variable | optional; Render free Postgres is 1024 |

`DATABASE_URL_DIRECT` is not needed — Render has no pooled endpoint of the
kind Neon and Lakebase use, so there is nothing to strip.

### 4. Confirm the secret arrives

**Actions → Secrets check → Run workflow.** It prints the names of the
secrets the workflow can see and the length of `DATABASE_URL`, never a
value. Expect a length in the low hundreds. Zero means the secret is not
reaching the workflow.

### 5. Load history, then schedule

**Actions → Database → backfill**, `history = 1000` — roughly four years of
daily candles per symbol. Run it at most once a day: Alpha Vantage's free
tier allows 25 requests daily and each backfill with three symbols uses
twelve.

Then commit `etl.yml` and run it once by hand. Green means the schedule
works: every 30 minutes during market hours, plus a nightly full refresh.

---

## Backup and restore

### Taking one

Weekly on its own, or **Actions → Backup → Run workflow** at any time.
There is a `schema_only` option for when you want the structure without the
rows.

The job dumps with `--format=custom --no-owner --no-privileges`. Those last
two matter: a new Render database has different role names, and without
them every `ALTER ... OWNER TO` line fails on restore.

It then runs `pg_restore --list` on the file. A dump that cannot be listed
is not a backup, and the job fails rather than uploading a corrupt archive.

### Restoring

Download the artifact from the run, create a new Render Postgres, and:

```bash
pg_restore --no-owner --no-privileges \
  --dbname "postgresql://USER:PASSWORD@NEW-HOST/DBNAME?sslmode=require" \
  market_pipeline-20261013-0410.dump
```

Then update the `DATABASE_URL` secret to the new database and carry on.
Nothing else changes.

Add `--clean --if-exists` if you are restoring over a database that already
has the tables.

This cycle was tested end to end on PostgreSQL 16, with real pipeline data:
dump, restore into an empty database, compare every table row for row
(identical), confirm the 16 views and 30 indexes came back, confirm the
sequences kept their values so new inserts do not collide, and run the
pipeline again against the restored database (succeeded).

---

## When the free month runs out

You have three options, and the pipeline needs no code changes for any of
them — only a new `DATABASE_URL`.

**Pay for Render Postgres.** Simplest. The database stops expiring.

**Move to a database with no expiry.** A free Neon or Supabase project does
not expire the way Render's does, though each has its own limits: Neon caps
storage at 0.5 GB per project and compute at 100 CU-hours a month; Supabase
pauses a project after a week of inactivity, which the 30-minute ETL
schedule prevents. Restore your latest dump there, change the secret, done.

**Start a new free Render database every month.** Works, but it is a manual
restore every 30 days and one forgotten month loses everything since the
last backup. Not recommended.

Whatever you choose, the backup workflow is what makes the choice reversible.

---

## Storage

Measured on this schema: about 389 bytes per candle row. With three symbols
and the default settings, roughly 5 MB a month — about 1.8 MB of it
5-minute candles, which is the single biggest consumer.

Render's free Postgres allows 1 GB, so storage is not the binding limit here
— the 30-day expiry is. `.github/workflows/maintenance.yml` runs retention
and `VACUUM` monthly anyway, which keeps growth flat over the long run.

To cut growth to under 2 MB a month, set the Actions variable
`INTRADAY_ENABLED` to `false`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no password supplied` | The client sent an empty password field; paste it and tick Save Password |
| `password authentication failed` | Wrong password, or the internal URL used from outside Render |
| `could not translate host name` | The internal URL is being used from GitHub Actions; use the external one |
| `SSL connection is required` | `?sslmode=require` is missing from the end |
| Workflow is green but tables stay empty | Check `log_api_call`; its `error_msg` column names the provider that refused |
| `relation "bi_..." does not exist` | The views were never created; run `Database → migrate` |
| Tables invisible in pgAdmin | Look under `Databases → <db> → Schemas → public → Tables`, and set Maintenance database to your database, not `postgres` |
| Scheduled runs stopped | A public repo's schedules are disabled after 60 days without a commit |

To see what actually happened:

```sql
SELECT started_at_utc, status, symbols_ok, symbols_failed, error_msg
  FROM log_pipeline_run ORDER BY run_id DESC LIMIT 10;

SELECT called_at_utc, endpoint, http_status, error_msg
  FROM log_api_call
 WHERE http_status IS NULL OR http_status >= 400
 ORDER BY called_at_utc DESC LIMIT 20;
```

---

## Sources

- [Render free plan — service types, spin-down, 30-day Postgres expiry](https://render.com/docs/free)
- [Render Postgres connection strings, internal vs external](https://render.com/docs/postgresql-connecting)
