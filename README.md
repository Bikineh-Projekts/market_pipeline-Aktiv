# Data export

Regenerated automatically. Every file here is replaced on each export, so
nothing in this folder is history -- it is the current contents of the
database.

| file | what it is |
| --- | --- |
| `market_data.xlsx` | the `bi_*` reporting views, one sheet each. For opening and reading. |
| `csv/*.csv.gz` | every raw table, gzipped and uncapped. For loading. |
| `manifest.json` | row counts and timestamps. Read this to tell whether anything changed. |

Every `*_local` column, and every `local` field in the manifest, is
**Europe/Berlin** wall-clock. Every `*_utc` column and `utc` field is the same
instant in UTC. Sort and compare on the UTC ones; display the local ones.

## Loading these into your own PostgreSQL

This loads into EMPTY tables -- for the first copy, or for rebuilding after
Render has deleted the database. It is **not** a way to stay up to date: it
has no idea what is already there, so running it twice fails on duplicate
keys. For that, see the replicator at the bottom.

```bash
LOCAL="postgresql://postgres:PASSWORD@localhost:5432/marketdb"

# 1. the schema, once
psql "$LOCAL" -f sql/01_schema.sql
psql "$LOCAL" -f sql/02_bi_views.sql

# 2. clear the seeded dimension rows.
#    01_schema.sql SEEDS dim_source, dim_interval and dim_indicator, so
#    those tables are NOT empty even in a brand new database, and the
#    first \copy fails on a duplicate source_name. Skipping this step is
#    the most likely reason the load below does not work.
psql "$LOCAL" -c \
  "TRUNCATE dim_source, dim_interval, dim_indicator, dim_symbol CASCADE"

# 3. the data, dimensions first -- a fact cannot reference a symbol that
#    is not there yet. This is the order manifest.json lists them in.
for t in dim_source dim_interval dim_indicator dim_symbol fact_market_timeseries fact_market_quote fact_market_indicator fact_company_fundamental fact_earnings_calendar log_pipeline_run log_api_call; do
  gzip -dc "csv/$t.csv.gz" \
    | psql "$LOCAL" -v ON_ERROR_STOP=1 \
        -c "\copy $t FROM STDIN WITH (FORMAT csv, HEADER true)"
done

# 4. move every sequence past the ids just loaded.
#    \copy writes the id column explicitly and does NOT advance the
#    sequence behind it, so without this the next row you insert
#    yourself collides with an existing id. That reads as a corrupt
#    database and is only a stale counter.
psql "$LOCAL" -Atc "
  SELECT format(
    'SELECT setval(%L, COALESCE((SELECT MAX(%I) FROM %I), 1), '
    'EXISTS (SELECT 1 FROM %I));',
    pg_get_serial_sequence(table_name, column_name),
    column_name, table_name, table_name)
    FROM information_schema.columns
   WHERE table_schema = 'public'
     AND pg_get_serial_sequence(table_name, column_name) IS NOT NULL
" | psql "$LOCAL" -q
```

## Keeping the copy up to date

The replicator copies only the rows the other side has not seen, so a pass
costs a few small queries when nothing changed, and running it twice
changes nothing:

```bash
python tools/replicate.py \
    --source "$RENDER_DATABASE_URL" \
    --target "$LOCAL" \
    --loop 10
```

It does step 4 for you on every pass. And because it only ever reads from
`--source` and writes to `--target`, swapping the two rebuilds a new Render
database from the local copy.
