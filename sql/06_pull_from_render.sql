-- =====================================================================
-- 06_pull_from_render.sql
--
-- Pull the data from Render into THIS database using nothing but
-- pgAdmin. No Python, no repository checkout, no command line.
--
-- Your PostgreSQL gets a connection of its own to the Render one
-- (postgres_fdw, which ships with every standard PostgreSQL install), so
-- your server does the fetching. Run pull_from_render() whenever you want
-- the newest rows. Calling it again costs almost nothing and never
-- duplicates anything.
--
--   HOW TO USE IT
--   1. Edit the four marked values in STEP 2 below. Nothing else.
--   2. pgAdmin -> your database -> Query Tool -> paste this whole file
--      -> Execute (F5). Once.
--   3. From then on:     SELECT * FROM pull_from_render();
--      To see how current you are:   SELECT * FROM render_sync_status;
--
-- Everything here is plain SQL on purpose. pgAdmin's Query Tool is not
-- psql: it does not understand \set, :variables, \copy or \echo, and a
-- file using them fails on the first line with a syntax error.
--
-- Needs: the 11 tables already here (sql/01_schema.sql), and a user that
-- may CREATE EXTENSION -- postgres can.
--
-- THE ONE THING THIS HAS TO GET RIGHT
-- symbol_id, source_id, interval_id and indicator_id are SERIAL columns,
-- assigned independently in each database. Render's symbol_id 2 and yours
-- are not the same company, and because 01_schema.sql SEEDS three of
-- those dimensions, they are guaranteed to differ. Copying a fact's raw
-- symbol_id across would file Apple's prices under Microsoft with no
-- error whatsoever: row counts would match, every constraint would hold,
-- the audit would pass, and every number in the report would be wrong.
--
-- So no id column is ever copied. Each is looked up through the
-- dimension's NATURAL key -- symbol_code, source_name, interval_code,
-- indicator_name -- which does mean the same thing on both sides. That is
-- what the double join on every INSERT below is for.
-- =====================================================================


-- =====================================================================
-- STEP 1  The extension
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS postgres_fdw;


-- =====================================================================
-- STEP 2  The link to Render  --  THE ONLY PART YOU EDIT
--
-- Render dashboard -> your Postgres -> Connections. Use the EXTERNAL
-- values, not the internal ones: the internal hostname resolves only
-- inside Render, and from your machine it simply does not exist.
--
-- Only TWO values actually need changing: the hostname and the password.
-- dbname and user below already match this project's Render database
-- (market_db_aktiv / market_db_aktiv_user); change them only if yours
-- differ. Keep the quotes on everything.
-- =====================================================================
DROP SERVER IF EXISTS render_src CASCADE;

CREATE SERVER render_src
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (
        -- pgAdmin: right-click the Render server -> Properties ->
        -- Connection -> "Host name/address". Or Render -> Connections.
        host    'dpg-xxxxxxxxxxxxxxxx-a.frankfurt-postgres.render.com',  -- CHANGE ME  <-- 1 of 2
        port    '5432',
        dbname  'market_db_aktiv',           -- already correct
        -- Not optional. Render refuses a connection without SSL, and the
        -- refusal reads as "server closed the connection unexpectedly",
        -- which says nothing about SSL and sends you looking elsewhere.
        sslmode 'require'
    );

-- The password is stored inside your own database, readable only by
-- superusers. It is never written to a table, a view, or a log.
CREATE USER MAPPING FOR CURRENT_USER
    SERVER render_src
    OPTIONS (
        user     'market_db_aktiv_user',     -- already correct
        password 'PASTE_THE_PASSWORD_HERE'   -- CHANGE ME  <-- 2 of 2
    );

CREATE SCHEMA IF NOT EXISTS render;

IMPORT FOREIGN SCHEMA public
    LIMIT TO (dim_source, dim_interval, dim_indicator, dim_symbol,
              fact_market_timeseries, fact_market_quote,
              fact_market_indicator, fact_company_fundamental,
              fact_earnings_calendar, log_pipeline_run, log_api_call)
    FROM SERVER render_src INTO render;


-- =====================================================================
-- STEP 3  Bookkeeping
-- =====================================================================

-- One row per table, holding the highest id already fetched from Render.
-- This is what makes a repeat call cheap -- and for log_api_call it is
-- the ONLY thing preventing duplicates, because that table has no unique
-- constraint by design: every API attempt is logged, retries included, so
-- two identical-looking rows are two real events and both must survive.
CREATE TABLE IF NOT EXISTS render_sync_state (
    table_name     TEXT PRIMARY KEY,
    last_remote_id BIGINT      NOT NULL DEFAULT 0,
    rows_copied    BIGINT      NOT NULL DEFAULT 0,
    last_run_utc   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE render_sync_state IS
    'How far pull_from_render() has read from each Render table. Deleting '
    'a row makes the next pull re-read that table from the beginning, '
    'which is safe for every table except log_api_call.';


CREATE OR REPLACE FUNCTION render_watermark(p_table TEXT, p_overlap BIGINT DEFAULT 0)
RETURNS BIGINT
LANGUAGE sql STABLE AS $$
    -- The overlap re-reads a few rows below the mark on every pass, for
    -- tables whose rows CHANGE after they were first written. A pipeline
    -- run is INSERTed as 'running' and UPDATEd to 'success' minutes
    -- later; with no overlap every run here would stay 'running' forever,
    -- which looks exactly like a pipeline that keeps dying.
    SELECT GREATEST(COALESCE((SELECT last_remote_id FROM render_sync_state
                               WHERE table_name = p_table), 0) - p_overlap, 0);
$$;


CREATE OR REPLACE FUNCTION render_remember(p_table TEXT, p_mark BIGINT, p_rows BIGINT)
RETURNS VOID
LANGUAGE sql AS $$
    INSERT INTO render_sync_state (table_name, last_remote_id, rows_copied, last_run_utc)
    VALUES (p_table, p_mark, p_rows, NOW())
    ON CONFLICT (table_name) DO UPDATE
        SET last_remote_id = GREATEST(render_sync_state.last_remote_id,
                                      EXCLUDED.last_remote_id),
            rows_copied    = render_sync_state.rows_copied + EXCLUDED.rows_copied,
            last_run_utc   = EXCLUDED.last_run_utc;
$$;


-- Move every sequence past the ids now present.
--
-- The INSERTs below let the sequence assign ids, so this is belt and
-- braces for them -- but it matters the moment you ever load a CSV export
-- by hand, because \copy writes the id column explicitly and leaves the
-- counter behind it. The next row you insert yourself then collides with
-- an existing id, which reads as a corrupt database and is only ever a
-- stale counter.
CREATE OR REPLACE FUNCTION render_fix_sequences()
RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT c.table_name AS t, c.column_name AS col,
               pg_get_serial_sequence(c.table_name, c.column_name) AS seq
          FROM information_schema.columns c
         WHERE c.table_schema = 'public'
           AND pg_get_serial_sequence(c.table_name, c.column_name) IS NOT NULL
    LOOP
        EXECUTE format(
            'SELECT setval(%L, COALESCE((SELECT MAX(%I) FROM %I), 1), '
            'EXISTS (SELECT 1 FROM %I))', r.seq, r.col, r.t, r.t);
    END LOOP;
END $$;


-- =====================================================================
-- STEP 4  The pull itself
--
-- Returns one row per table saying how many rows it added, so the Query
-- Tool shows you at a glance whether anything arrived.
-- =====================================================================
CREATE OR REPLACE FUNCTION pull_from_render()
RETURNS TABLE (table_name TEXT, rows_added BIGINT,
               rows_updated BIGINT, read_up_to BIGINT)
LANGUAGE plpgsql AS $$
DECLARE
    n    BIGINT;   -- rows inserted
    u    BIGINT;   -- rows updated in place
    mark BIGINT;
BEGIN
    -- ---------------- dimensions, matched on their natural key --------
    -- Every count below separates rows INSERTED from rows UPDATED in
    -- place, via `xmax = 0` -- true only for a row this statement itself
    -- inserted. GET DIAGNOSTICS ROW_COUNT cannot tell them apart, and
    -- reporting them together was actively misleading: the two tables
    -- with an ON CONFLICT DO UPDATE re-read a few rows on every pass by
    -- design, so a pull with nothing whatsoever to do announced
    -- "fact_earnings_calendar: 6, log_pipeline_run: 3". A status display
    -- that reports arrivals that did not happen is worse than none, and
    -- this one exists precisely to answer "is anything arriving?".
    INSERT INTO dim_source (source_name, base_url, notes)
    SELECT d.source_name, d.base_url, d.notes FROM render.dim_source d
    ON CONFLICT (source_name) DO UPDATE
        SET base_url = EXCLUDED.base_url, notes = EXCLUDED.notes;

    INSERT INTO dim_interval (interval_code, interval_type)
    SELECT d.interval_code, d.interval_type FROM render.dim_interval d
    ON CONFLICT (interval_code) DO UPDATE
        SET interval_type = EXCLUDED.interval_type;

    INSERT INTO dim_indicator (indicator_name, description, category)
    SELECT d.indicator_name, d.description, d.category FROM render.dim_indicator d
    ON CONFLICT (indicator_name) DO UPDATE
        SET description = EXCLUDED.description, category = EXCLUDED.category;

    -- This one must UPDATE, not DO NOTHING. A symbol is first written
    -- when a quote arrives, carrying no profile at all; the company name
    -- and sector turn up on a later run. DO NOTHING here would leave them
    -- NULL forever and every Power BI slicer built on them would read
    -- "Uncategorized" -- which is exactly the bug this project already
    -- had once, on the other side.
    INSERT INTO dim_symbol (symbol_code, company_name, exchange, country,
                            currency, sector, industry)
    SELECT d.symbol_code, d.company_name, d.exchange, d.country,
           d.currency, d.sector, d.industry
      FROM render.dim_symbol d
    ON CONFLICT (symbol_code) DO UPDATE
        SET company_name = COALESCE(EXCLUDED.company_name, dim_symbol.company_name),
            exchange     = COALESCE(EXCLUDED.exchange,     dim_symbol.exchange),
            country      = COALESCE(EXCLUDED.country,      dim_symbol.country),
            currency     = COALESCE(EXCLUDED.currency,     dim_symbol.currency),
            sector       = COALESCE(EXCLUDED.sector,       dim_symbol.sector),
            industry     = COALESCE(EXCLUDED.industry,     dim_symbol.industry);

    -- ---------------- candles -----------------------------------------
    mark := render_watermark('fact_market_timeseries');
    WITH written AS (
    INSERT INTO fact_market_timeseries
        (symbol_id, source_id, interval_id, candle_time_utc,
         open, high, low, close, volume, raw_payload)
    SELECT ls.symbol_id, lsrc.source_id, li.interval_id, r.candle_time_utc,
           r.open, r.high, r.low, r.close, r.volume, r.raw_payload
      FROM render.fact_market_timeseries r
      JOIN render.dim_symbol   rs   ON rs.symbol_id      = r.symbol_id
      JOIN dim_symbol          ls   ON ls.symbol_code    = rs.symbol_code
      JOIN render.dim_source   rsrc ON rsrc.source_id    = r.source_id
      JOIN dim_source          lsrc ON lsrc.source_name  = rsrc.source_name
      JOIN render.dim_interval ri   ON ri.interval_id    = r.interval_id
      JOIN dim_interval        li   ON li.interval_code  = ri.interval_code
     WHERE r.timeseries_id > mark
    ON CONFLICT (symbol_id, interval_id, candle_time_utc) DO NOTHING
    RETURNING (xmax = 0) AS was_inserted
)
SELECT COUNT(*) FILTER (WHERE was_inserted),
       COUNT(*) FILTER (WHERE NOT was_inserted)
  INTO n, u FROM written;
    mark := GREATEST(mark, COALESCE(
        (SELECT MAX(r.timeseries_id) FROM render.fact_market_timeseries r), 0));
    PERFORM render_remember('fact_market_timeseries', mark, n);
    table_name := 'fact_market_timeseries'; rows_added := n; rows_updated := u; read_up_to := mark;
    RETURN NEXT;

    -- ---------------- quotes ------------------------------------------
    mark := render_watermark('fact_market_quote');
    WITH written AS (
    INSERT INTO fact_market_quote
            (symbol_id, source_id, fetched_at_utc, quote_time_utc, price,
             open, high, low, previous_close, change, change_pct, raw_payload)
        SELECT ls.symbol_id, lsrc.source_id, r.fetched_at_utc, r.quote_time_utc,
               r.price, r.open, r.high, r.low, r.previous_close,
               r.change, r.change_pct, r.raw_payload
          FROM render.fact_market_quote r
          JOIN render.dim_symbol rs   ON rs.symbol_id     = r.symbol_id
          JOIN dim_symbol        ls   ON ls.symbol_code   = rs.symbol_code
          JOIN render.dim_source rsrc ON rsrc.source_id   = r.source_id
          JOIN dim_source        lsrc ON lsrc.source_name = rsrc.source_name
         WHERE r.quote_id > mark
        ON CONFLICT (symbol_id, source_id, quote_time_utc) DO NOTHING
        RETURNING (xmax = 0) AS was_inserted
    )
    SELECT COUNT(*) FILTER (WHERE was_inserted),
           COUNT(*) FILTER (WHERE NOT was_inserted)
      INTO n, u FROM written;
    mark := GREATEST(mark, COALESCE(
        (SELECT MAX(r.quote_id) FROM render.fact_market_quote r), 0));
    PERFORM render_remember('fact_market_quote', mark, n);
    table_name := 'fact_market_quote'; rows_added := n; rows_updated := u; read_up_to := mark;
    RETURN NEXT;

    -- ---------------- indicators --------------------------------------
    mark := render_watermark('fact_market_indicator');
    WITH written AS (
    INSERT INTO fact_market_indicator
            (symbol_id, source_id, indicator_id, interval_id, candle_time_utc,
             value, macd, macd_signal, macd_hist, raw_payload)
        SELECT ls.symbol_id, lsrc.source_id, lind.indicator_id, li.interval_id,
               r.candle_time_utc, r.value, r.macd, r.macd_signal, r.macd_hist,
               r.raw_payload
          FROM render.fact_market_indicator r
          JOIN render.dim_symbol    rs   ON rs.symbol_id        = r.symbol_id
          JOIN dim_symbol           ls   ON ls.symbol_code      = rs.symbol_code
          JOIN render.dim_source    rsrc ON rsrc.source_id      = r.source_id
          JOIN dim_source           lsrc ON lsrc.source_name    = rsrc.source_name
          JOIN render.dim_indicator rind ON rind.indicator_id   = r.indicator_id
          JOIN dim_indicator        lind ON lind.indicator_name = rind.indicator_name
          JOIN render.dim_interval  ri   ON ri.interval_id      = r.interval_id
          JOIN dim_interval         li   ON li.interval_code    = ri.interval_code
         WHERE r.indicator_fact_id > mark
        ON CONFLICT (symbol_id, indicator_id, interval_id, candle_time_utc) DO NOTHING
        RETURNING (xmax = 0) AS was_inserted
    )
    SELECT COUNT(*) FILTER (WHERE was_inserted),
           COUNT(*) FILTER (WHERE NOT was_inserted)
      INTO n, u FROM written;
    mark := GREATEST(mark, COALESCE(
        (SELECT MAX(r.indicator_fact_id) FROM render.fact_market_indicator r), 0));
    PERFORM render_remember('fact_market_indicator', mark, n);
    table_name := 'fact_market_indicator'; rows_added := n; rows_updated := u; read_up_to := mark;
    RETURN NEXT;

    -- ---------------- fundamentals ------------------------------------
    mark := render_watermark('fact_company_fundamental', 20);
    WITH written AS (
    INSERT INTO fact_company_fundamental
            (symbol_id, source_id, fetched_at_utc, fetched_date, ipo_date,
             market_cap, share_outstanding, pe_ratio, eps_ttm, gross_margin,
             net_margin, roe, debt_to_equity, current_ratio, beta,
             week_52_high, week_52_low, raw_profile, raw_metrics)
        SELECT ls.symbol_id, lsrc.source_id, r.fetched_at_utc, r.fetched_date,
               r.ipo_date, r.market_cap, r.share_outstanding, r.pe_ratio,
               r.eps_ttm, r.gross_margin, r.net_margin, r.roe, r.debt_to_equity,
               r.current_ratio, r.beta, r.week_52_high, r.week_52_low,
               r.raw_profile, r.raw_metrics
          FROM render.fact_company_fundamental r
          JOIN render.dim_symbol rs   ON rs.symbol_id     = r.symbol_id
          JOIN dim_symbol        ls   ON ls.symbol_code   = rs.symbol_code
          JOIN render.dim_source rsrc ON rsrc.source_id   = r.source_id
          JOIN dim_source        lsrc ON lsrc.source_name = rsrc.source_name
         WHERE r.fundamental_id > mark
        ON CONFLICT (symbol_id, source_id, fetched_date) DO NOTHING
        RETURNING (xmax = 0) AS was_inserted
    )
    SELECT COUNT(*) FILTER (WHERE was_inserted),
           COUNT(*) FILTER (WHERE NOT was_inserted)
      INTO n, u FROM written;
    mark := GREATEST(mark, COALESCE(
        (SELECT MAX(r.fundamental_id) FROM render.fact_company_fundamental r), 0));
    PERFORM render_remember('fact_company_fundamental', mark, n);
    table_name := 'fact_company_fundamental'; rows_added := n; rows_updated := u; read_up_to := mark;
    RETURN NEXT;

    -- ---------------- earnings ----------------------------------------
    -- Overlap 50: an estimate becomes an actual when the company
    -- reports, so these rows change long after they were first written.
    mark := render_watermark('fact_earnings_calendar', 50);
    WITH written AS (
    INSERT INTO fact_earnings_calendar
            (symbol_id, source_id, fetched_at_utc, report_date, hour,
             eps_estimate, eps_actual, revenue_estimate, revenue_actual, raw_payload)
        SELECT ls.symbol_id, lsrc.source_id, r.fetched_at_utc, r.report_date,
               r.hour, r.eps_estimate, r.eps_actual, r.revenue_estimate,
               r.revenue_actual, r.raw_payload
          FROM render.fact_earnings_calendar r
          JOIN render.dim_symbol rs   ON rs.symbol_id     = r.symbol_id
          JOIN dim_symbol        ls   ON ls.symbol_code   = rs.symbol_code
          JOIN render.dim_source rsrc ON rsrc.source_id   = r.source_id
          JOIN dim_source        lsrc ON lsrc.source_name = rsrc.source_name
         WHERE r.earnings_id > mark
        ON CONFLICT (symbol_id, report_date) DO UPDATE
            SET eps_actual     = COALESCE(EXCLUDED.eps_actual,
                                          fact_earnings_calendar.eps_actual),
                revenue_actual = COALESCE(EXCLUDED.revenue_actual,
                                          fact_earnings_calendar.revenue_actual),
                eps_estimate   = COALESCE(EXCLUDED.eps_estimate,
                                          fact_earnings_calendar.eps_estimate)
            -- Only where something genuinely differs. The overlap above
            -- re-reads these rows on EVERY pass; without this they would
            -- be rewritten with identical values forever and an idle pull
            -- would keep announcing "6 updated".
            --
            -- IS DISTINCT FROM, not <>: these columns are routinely NULL,
            -- and NULL <> NULL is NULL rather than true, so a plain <>
            -- would skip the row at exactly the moment that matters --
            -- when an estimate first becomes an actual.
            WHERE fact_earnings_calendar.eps_actual
                      IS DISTINCT FROM COALESCE(EXCLUDED.eps_actual,
                                                fact_earnings_calendar.eps_actual)
               OR fact_earnings_calendar.revenue_actual
                      IS DISTINCT FROM COALESCE(EXCLUDED.revenue_actual,
                                                fact_earnings_calendar.revenue_actual)
               OR fact_earnings_calendar.eps_estimate
                      IS DISTINCT FROM COALESCE(EXCLUDED.eps_estimate,
                                                fact_earnings_calendar.eps_estimate)
        RETURNING (xmax = 0) AS was_inserted
    )
    SELECT COUNT(*) FILTER (WHERE was_inserted),
           COUNT(*) FILTER (WHERE NOT was_inserted)
      INTO n, u FROM written;
    mark := GREATEST(mark, COALESCE(
        (SELECT MAX(r.earnings_id) FROM render.fact_earnings_calendar r), 0));
    PERFORM render_remember('fact_earnings_calendar', mark, n);
    table_name := 'fact_earnings_calendar'; rows_added := n; rows_updated := u; read_up_to := mark;
    RETURN NEXT;

    -- ---------------- pipeline runs -----------------------------------
    mark := render_watermark('log_pipeline_run', 10);
    WITH written AS (
    INSERT INTO log_pipeline_run
            (run_uid, trigger_source, started_at_utc, finished_at_utc,
             duration_seconds, status, symbols_total, symbols_ok,
             symbols_failed, rows_written, error_msg)
        SELECT r.run_uid, r.trigger_source, r.started_at_utc, r.finished_at_utc,
               r.duration_seconds, r.status, r.symbols_total, r.symbols_ok,
               r.symbols_failed, r.rows_written, r.error_msg
          FROM render.log_pipeline_run r
         WHERE r.run_id > mark
        ON CONFLICT (run_uid) DO UPDATE
            SET finished_at_utc  = EXCLUDED.finished_at_utc,
                duration_seconds = EXCLUDED.duration_seconds,
                status           = EXCLUDED.status,
                symbols_ok       = EXCLUDED.symbols_ok,
                symbols_failed   = EXCLUDED.symbols_failed,
                rows_written     = EXCLUDED.rows_written,
                error_msg        = EXCLUDED.error_msg
            -- Same reason as the earnings table: the overlap re-reads the
            -- last ten runs every pass, and a run that finished hours ago
            -- has nothing left to change.
            WHERE log_pipeline_run.status          IS DISTINCT FROM EXCLUDED.status
               OR log_pipeline_run.finished_at_utc IS DISTINCT FROM EXCLUDED.finished_at_utc
               OR log_pipeline_run.rows_written    IS DISTINCT FROM EXCLUDED.rows_written
               OR log_pipeline_run.symbols_ok      IS DISTINCT FROM EXCLUDED.symbols_ok
               OR log_pipeline_run.symbols_failed  IS DISTINCT FROM EXCLUDED.symbols_failed
               OR log_pipeline_run.error_msg       IS DISTINCT FROM EXCLUDED.error_msg
        RETURNING (xmax = 0) AS was_inserted
    )
    SELECT COUNT(*) FILTER (WHERE was_inserted),
           COUNT(*) FILTER (WHERE NOT was_inserted)
      INTO n, u FROM written;
    mark := GREATEST(mark, COALESCE(
        (SELECT MAX(r.run_id) FROM render.log_pipeline_run r), 0));
    PERFORM render_remember('log_pipeline_run', mark, n);
    table_name := 'log_pipeline_run'; rows_added := n; rows_updated := u; read_up_to := mark;
    RETURN NEXT;

    -- ---------------- api calls ---------------------------------------
    -- No overlap and no ON CONFLICT, because there is nothing to conflict
    -- on. The watermark is the only protection here, which is why this is
    -- the one table where clearing render_sync_state duplicates instead
    -- of re-syncing.
    --
    -- symbol_id is nullable -- a call need not concern any one symbol --
    -- so these are LEFT JOINs. An inner join would silently drop exactly
    -- those rows and quietly understate the API usage.
    mark := render_watermark('log_api_call');
    WITH written AS (
    INSERT INTO log_api_call
            (source_id, symbol_id, called_at_utc, endpoint,
             http_status, response_ms, error_msg)
        SELECT lsrc.source_id, ls.symbol_id, r.called_at_utc, r.endpoint,
               r.http_status, r.response_ms, r.error_msg
          FROM render.log_api_call r
          JOIN      render.dim_source rsrc ON rsrc.source_id   = r.source_id
          JOIN      dim_source        lsrc ON lsrc.source_name = rsrc.source_name
          LEFT JOIN render.dim_symbol rs   ON rs.symbol_id     = r.symbol_id
          LEFT JOIN dim_symbol        ls   ON ls.symbol_code   = rs.symbol_code
         WHERE r.log_id > mark
        RETURNING (xmax = 0) AS was_inserted
    )
    SELECT COUNT(*) FILTER (WHERE was_inserted),
           COUNT(*) FILTER (WHERE NOT was_inserted)
      INTO n, u FROM written;
    mark := GREATEST(mark, COALESCE(
        (SELECT MAX(r.log_id) FROM render.log_api_call r), 0));
    PERFORM render_remember('log_api_call', mark, n);
    table_name := 'log_api_call'; rows_added := n; rows_updated := u; read_up_to := mark;
    RETURN NEXT;

    PERFORM render_fix_sequences();
    RETURN;
END $$;


-- =====================================================================
-- STEP 5  "Is my copy current?", in one query
--
-- Berlin time is written out here rather than calling local_ts(), so
-- this file also works on a database created before those functions
-- existed -- which is the case for tables made by hand in pgAdmin.
-- =====================================================================
CREATE OR REPLACE VIEW render_sync_status AS
SELECT
    s.table_name,
    s.last_remote_id,
    s.rows_copied,
    s.last_run_utc                                        AS last_pull_utc,
    (s.last_run_utc AT TIME ZONE 'Europe/Berlin')          AS last_pull_berlin,
    ROUND(EXTRACT(EPOCH FROM (NOW() - s.last_run_utc)) / 60.0, 1)
                                                           AS minutes_since_pull
FROM render_sync_state s;


-- =====================================================================
-- STEP 5b  Preflight
--
-- Every INSERT below says ON CONFLICT (...), and PostgreSQL can only
-- honour that if a UNIQUE constraint covers exactly those columns. Your
-- tables were created by hand from an earlier copy of 01_schema.sql, so
-- one may be missing -- and the error you would get is
--
--     there is no unique or exclusion constraint matching the
--     ON CONFLICT specification
--
-- which names neither the table nor the columns and leaves you nowhere.
-- This names both, and gives you the line that fixes it.
--
-- It only reports. Adding the constraint itself could fail on data
-- already duplicated, and silently deleting rows to force it through is
-- not a thing a setup script should do behind your back.
-- =====================================================================
DO $preflight$
DECLARE
    want   RECORD;
    missed TEXT := '';
BEGIN
    FOR want IN
        SELECT * FROM (VALUES
            ('fact_market_quote',        ARRAY['quote_time_utc','source_id','symbol_id'],
             'uq_fact_quote',       'symbol_id, source_id, quote_time_utc'),
            ('fact_market_timeseries',   ARRAY['candle_time_utc','interval_id','symbol_id'],
             'uq_fact_timeseries',  'symbol_id, interval_id, candle_time_utc'),
            ('fact_market_indicator',    ARRAY['candle_time_utc','indicator_id','interval_id','symbol_id'],
             'uq_fact_indicator',   'symbol_id, indicator_id, interval_id, candle_time_utc'),
            ('fact_company_fundamental', ARRAY['fetched_date','source_id','symbol_id'],
             'uq_fact_fundamental', 'symbol_id, source_id, fetched_date'),
            ('fact_earnings_calendar',   ARRAY['report_date','symbol_id'],
             'uq_fact_earnings',    'symbol_id, report_date'),
            ('log_pipeline_run',         ARRAY['run_uid'],
             'uq_run_uid',          'run_uid')
        ) AS v(tbl, cols, suggested_name, col_list)
    LOOP
        IF to_regclass('public.' || want.tbl) IS NULL THEN
            missed := missed || format(
                E'\n  table %s does not exist. Run sql/01_schema.sql first.', want.tbl);
            CONTINUE;
        END IF;

        IF NOT EXISTS (
            SELECT 1
              FROM pg_constraint c
             WHERE c.conrelid = ('public.' || want.tbl)::regclass
               AND c.contype IN ('u', 'p')
               AND (SELECT array_agg(a.attname::text ORDER BY a.attname)
                      FROM unnest(c.conkey) k
                      JOIN pg_attribute a ON a.attrelid = c.conrelid
                                         AND a.attnum   = k)
                   = (SELECT array_agg(x ORDER BY x) FROM unnest(want.cols) x)
        ) THEN
            missed := missed || format(
                E'\n  ALTER TABLE %s ADD CONSTRAINT %s UNIQUE (%s);',
                want.tbl, want.suggested_name, want.col_list);
        END IF;
    END LOOP;

    IF missed <> '' THEN
        RAISE EXCEPTION
            E'This database is missing constraints the pull needs.\n'
            -- RAISE's placeholder is %, not %s -- format() uses %s, RAISE
            -- does not, and writing %s here printed a stray 's'.
            'Run these, then run this file again:%', missed;
    END IF;

    RAISE NOTICE 'Preflight passed: every constraint the pull needs is present.';
END $preflight$;


-- =====================================================================
-- STEP 6  Pull once, now, so you can see whether it worked
-- =====================================================================
SELECT * FROM pull_from_render();
