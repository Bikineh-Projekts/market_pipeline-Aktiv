# Keeping a live copy in your own PostgreSQL

The goal: the data is always also on your machine, so that when Render
deletes the free database — which it does, 30 days after it is created —
you lose nothing, and can rebuild from your copy without interrupting
either the backup or the pipeline.

## Nothing arrives on its own — read this first

**No part of this project can push data into your PostgreSQL.** Not the
ETL, not Render, not GitHub Actions. Your database listens on your own
machine, behind your own router; everything else here is on the public
internet with no route to it, no credentials for it, and no knowledge that
it exists. That is not a limitation to be fixed — a database anyone on the
internet could write to is a database anyone on the internet could empty.

So data appears in your Postgres only when something **on your machine**
goes and fetches it. That something is `tools/replicate.py`, and until you
start it, your tables stay exactly as empty as the day you created them,
however healthy the Render side is.

If nothing has arrived, the first question is not "what broke" — it is
"is the replicator running". Start here:

```bash
python tools/local_setup.py \
    --source "postgresql://...render.com/market_db_aktiv" \
    --target "postgresql://postgres:PASSWORD@localhost:5432/marketdb"
```

That creates the tables, fills them once, and tells you what to run to
keep them current. It is safe to run again, and it explains what to do
rather than printing a stack trace when it cannot connect.

---

Two paths do this, and they are for different things.

| | `tools/replicate.py` | the `data-export` branch |
| --- | --- | --- |
| what it is | database → database, directly | files at a fixed URL |
| how current | seconds | minutes to hours |
| runs on | your machine | GitHub Actions |
| copies | only the rows you do not have | everything, every time |
| needs | your machine to be on | nothing |
| use it for | the live copy | Excel, and a fallback |

**Use the replicator for the live copy.** Use the files for opening the
data in Excel, and as something downloadable when your machine has been
off or the database is gone.

---

## About "every second"

A file in GitHub cannot be refreshed every second, and it is worth being
precise about why, because the limit is not one that can be worked around:

- the documented minimum for a scheduled workflow is **every 5 minutes**;
- scheduled runs are **best-effort** and are routinely delayed several
  minutes under load, so the real spacing is more like 5–20 minutes;
- a run spends ~40 seconds on checkout and `pip install` before it reads a
  single row, so sub-minute intervals would overlap each other.

And it would buy nothing even if it worked. **The data only changes when
the ETL runs** — every 30 minutes during market hours, bounded by the API
quotas (Alpha Vantage allows 25 requests a *day*). A per-second export
would publish about 1,800 byte-identical copies between two real updates.

So the export is triggered by **the ETL finishing**, which is the moment
the data actually changed, plus a twice-daily cron as a safety net.

What *can* run every second is the replicator, because it is on your
machine and talks to the database directly. A pass that finds nothing new
costs a handful of small queries — measured at **~35 ms** against a
database on the same machine, and a few hundred milliseconds against
Render over the internet. `--loop 10` is a sensible setting; `--loop 1`
works and is mostly idle. There is no point going below the ETL's own
cadence except to pick up a manual run quickly.

---

## Setting up the live copy

### 1. Create the schema locally, once

```bash
LOCAL="postgresql://postgres:YOUR_PASSWORD@localhost:5432/marketdb"

createdb marketdb                       # if it does not exist yet
psql "$LOCAL" -f sql/01_schema.sql
psql "$LOCAL" -f sql/02_bi_views.sql
```

If you already created the tables by hand, run those two files anyway —
both are idempotent, and `02_bi_views.sql` adds the 16 `bi_*` views and
the `local_ts` / `local_date` functions that everything else relies on.

### 2. Get the Render connection string

Render dashboard → your Postgres → **Connections** → **External Database
URL**. The *external* one: the internal URL only resolves inside Render's
network.

### 3. Run it

```bash
export SOURCE_DATABASE_URL="postgresql://...render.com/marketdb"
export LOCAL_DATABASE_URL="$LOCAL"

# see what it would do, without writing
python tools/replicate.py --once --dry-run

# one pass
python tools/replicate.py --once

# keep it current, forever
python tools/replicate.py --loop 10

# ...and keep the dashboard from spinning down while you are at it
python tools/replicate.py --loop 10 \
    --keep-awake https://market-pipeline-aktiv.onrender.com
```

Running it again never duplicates anything, so there is no harm in
stopping and starting it, or running it twice by accident.

**On `--keep-awake`.** A free Render *web service* spins down after 15
minutes without traffic and takes about a minute to wake up again. The
*database* does not — Render's free plan spins down web services only — so
this changes nothing about whether your data keeps flowing. It is about
the status page opening instantly instead of after a minute.

There is a keepalive workflow in GitHub Actions doing the same job, and
this option exists because that one cannot be depended on: GitHub says
plainly that scheduled runs are best effort and may be delayed or dropped,
and on this repository most of them are dropped. A loop already running on
your own machine is not best effort.

It pings once every 10 minutes, and only between 06:00 and 20:59 UTC. Both
limits are deliberate. The free plan allows **750 instance-hours a month**,
and a service kept awake around the clock spends about 744 of them — the
entire allowance, for a page nobody is reading at 04:00. The daytime window
costs about 465.

### 4. Leave it running

**On Windows**, open `tools\local_sync.cmd` in Notepad, put your two
connection strings at the top, save, and double-click it. It checks that
Python is on PATH, installs `psycopg2-binary` if it is missing, and keeps
copying until you close the window — restarting itself if the process
dies, which is the failure that matters: a copy that quietly stopped three
days ago still looks like it is working.

To have it start without you, Task Scheduler → Create Task → *Run whether
the user is logged on or not* → Action: `tools\local_sync.cmd`.

**On Linux**, as a user service that restarts itself:

```ini
# ~/.config/systemd/user/market-replicate.service
[Unit]
Description=Copy the market pipeline database to the local one

[Service]
Environment=SOURCE_DATABASE_URL=postgresql://...render.com/marketdb
Environment=LOCAL_DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/marketdb
ExecStart=/usr/bin/python3 /path/to/market_pipeline-aktiv/tools/replicate.py --loop 10
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now market-replicate
journalctl --user -u market-replicate -f
```

A connection error is logged and the next pass retries. Not because the
database sleeps — Render spins down free *web services*, not free
databases — but because a long connection over the public internet drops
sooner or later, and a laptop lid closes. The
watermark lives in the **target**, so a pass that is lost costs nothing:
the next one picks up exactly where the last successful one stopped.

---

## What it does, and the one thing it has to get right

`symbol_id`, `source_id`, `interval_id` and `indicator_id` are `SERIAL`
columns, so the same symbol can be id 2 in one database and id 7 in the
other. And it will be: `01_schema.sql` **seeds** `dim_source`,
`dim_interval` and `dim_indicator`, so your brand-new local database
already holds those rows under ids it assigned itself.

Copying a price row's raw `symbol_id` into that database attaches the
price to the wrong company — with no error, because the id exists and the
foreign key is satisfied. Row counts match. The audit passes. Every number
in the report is wrong.

So the replicator matches dimensions on their **natural** key
(`symbol_code`, `source_name`, `interval_code`, `indicator_name`) and
translates every fact's foreign keys through that mapping. Surrogate ids
are never copied. `tests/test_replication.py` checks this by renumbering
the target's dimensions on purpose and then comparing the data by
`symbol_code`.

Two other things worth knowing:

- It writes one extra table, `replication_state`, holding the high-water
  mark per table. That is how a pass knows what it has already copied.
  Nothing else in the schema changes.
- It opens the source **read-only** at the server, so a bug in it cannot
  damage the Render database.

---

## Rebuilding after Render deletes the database

This is what the copy is for. The replicator only ever reads from
`--source` and writes to `--target`, so swapping them turns the backup
into a restore.

```bash
# 1. New Postgres on Render. Copy its External Database URL.

# 2. Create the schema in it.
NEW="postgresql://...new-host.render.com/marketdb"
psql "$NEW" -f sql/01_schema.sql
psql "$NEW" -f sql/02_bi_views.sql

# 3. Push your local copy into it.
python tools/replicate.py --source "$LOCAL" --target "$NEW" --full

# 4. Check it.
psql "$NEW" -f sql/04_audit.sql
```

Then update the secret and the service, in **both** places — they are
separate stores and neither can see the other:

- **GitHub** → Settings → Secrets and variables → Actions → `DATABASE_URL`
  (this is what the ETL uses)
- **Render** → your web service → Environment → `DATABASE_URL`
  (this is what the dashboard uses)

Finally, point the replicator's `SOURCE_DATABASE_URL` at the new host.

Nothing else changes: the repository, the workflows and the Power BI model
all reach the database through that one secret.

### The alternative: restore from a dump

The Backup workflow writes a `pg_dump` archive to an Actions artifact every
Sunday, kept 90 days. If your local copy is also gone:

```bash
gh run download <run-id> -n db-backup-<run-id>
pg_restore --no-owner --no-privileges --clean --if-exists \
    --dbname "$NEW" market_pipeline-*.dump
```

A dump restores the surrogate ids exactly as they were, which the
replicator deliberately does not — for a restore that is what you want.

---

## Using the published files instead

Public repository, so no token is needed:

```bash
RAW=https://raw.githubusercontent.com/Bikineh-Projekts/market_pipeline-Aktiv/data-export

curl -fsSL "$RAW/manifest.json" | python -m json.tool   # what is in there
curl -fL   -o market_data.xlsx "$RAW/market_data.xlsx"  # the workbook
curl -fL   -o candles.csv.gz "$RAW/csv/fact_market_timeseries.csv.gz"
```

`manifest.json` carries row counts and timestamps, so a script can tell
whether anything changed before downloading the rest.

Loading the CSVs into an empty database is documented in the `README.md`
on that branch — including the two steps that are easy to miss (clearing
the seeded dimension rows first, and moving the sequences afterwards).

### A caveat about the workbook

An Excel worksheet holds **1,048,576 rows**, and that is a format limit,
not a setting. A sheet that would exceed it is truncated to the newest
rows, and the truncation is recorded in the sheet itself, in
`manifest.json` under `truncated_sheets`, and in the workflow log. The
CSV files are never truncated.

---

## All times are Berlin time

Instants are **stored** as `TIMESTAMPTZ`, which is an absolute point in
time and carries no zone. Only the display is converted. That is
deliberate: on the last Sunday in October the Berlin wall clock reads
02:00–03:00 twice, so a naive local timestamp inside that hour is genuinely
ambiguous and no longer sorts correctly.

So every view exposes each instant twice:

- `<name>_utc` — the absolute instant. **Sort and compare on this.**
- `<name>_local` — the same instant on the Berlin wall clock, as a naive
  timestamp. **Display this**, and build Power BI visuals on it: Power BI
  has no time-zone type, so a `TIMESTAMPTZ` arrives converted to whatever
  zone the report server runs in, and only the naive column shows the same
  clock to everyone.

`date_key` and every `*_date` column are derived from the **Berlin** day,
not the UTC day. A quote fetched at 23:30 UTC belongs to the next Berlin
day, and filing it under the UTC day would put it in the wrong bucket
every night.

Set `APP_TIMEZONE` to report in another zone, and change the literal in
`app_timezone()` in `sql/01_schema.sql` to match.
