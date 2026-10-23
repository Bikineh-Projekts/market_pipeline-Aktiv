-- =====================================================================
-- 01_schema.sql — base tables, indexes, seed data
--
-- Idempotent: safe to run any number of times, never destroys data.
--   psql "$DATABASE_URL" -f sql/01_schema.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- TIME ZONE
--
-- Instants are stored as TIMESTAMPTZ, which is an absolute point in time
-- and carries no zone of its own. Only the display zone is configurable,
-- and these three functions are the only place it is named.
--
-- Do not be tempted to store local time instead. On the last Sunday in
-- October the Berlin wall clock reads 02:00-03:00 twice; a naive local
-- timestamp inside that hour is genuinely ambiguous, no longer sorts
-- correctly, and silently breaks every "latest row" query. Store the
-- instant, display it in Berlin.
--
-- To report in another zone, change the literal here and re-run this file
-- followed by 02_bi_views.sql. Set APP_TIMEZONE to the same value so the
-- Python side and the dashboard agree.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_timezone() RETURNS TEXT
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT 'Europe/Berlin'::text $$;

-- The wall clock in that zone, as a naive timestamp. Naive on purpose:
-- Power BI has no time-zone type, so a TIMESTAMPTZ arrives converted to
-- whatever zone the report server happens to run in. A naive value is the
-- only way to make the report show the same clock for everyone.
CREATE OR REPLACE FUNCTION local_ts(ts TIMESTAMPTZ) RETURNS TIMESTAMP
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT ts AT TIME ZONE app_timezone() $$;

-- The calendar day that instant falls on locally. This is what decides
-- which day a row is counted in: a quote fetched at 23:30 UTC belongs to
-- the NEXT Berlin day, and using the UTC date files it under the wrong one.
CREATE OR REPLACE FUNCTION local_date(ts TIMESTAMPTZ) RETURNS DATE
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT (ts AT TIME ZONE app_timezone())::date $$;

CREATE OR REPLACE FUNCTION local_date_key(ts TIMESTAMPTZ) RETURNS INT
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT TO_CHAR(ts AT TIME ZONE app_timezone(), 'YYYYMMDD')::int $$;

-- Today, and now, in the display zone -- independent of the session's own
-- time zone, so a psql session or a pooled connection that forgot the
-- startup option still agrees with the dashboard.
CREATE OR REPLACE FUNCTION local_today() RETURNS DATE
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT (NOW() AT TIME ZONE app_timezone())::date $$;

-- ---------------------------------------------------------------------
-- DIMENSIONS
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_source (
    source_id   SERIAL PRIMARY KEY,
    source_name TEXT NOT NULL UNIQUE,
    base_url    TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS dim_symbol (
    symbol_id    SERIAL PRIMARY KEY,
    symbol_code  TEXT NOT NULL UNIQUE,
    company_name TEXT,
    exchange     TEXT,
    country      TEXT,
    currency     TEXT,
    sector       TEXT,
    industry     TEXT
);

CREATE TABLE IF NOT EXISTS dim_interval (
    interval_id   SERIAL PRIMARY KEY,
    interval_code TEXT NOT NULL UNIQUE,
    interval_type TEXT
);

CREATE TABLE IF NOT EXISTS dim_indicator (
    indicator_id   SERIAL PRIMARY KEY,
    indicator_name TEXT NOT NULL UNIQUE,
    description    TEXT,
    category       TEXT
);

-- ---------------------------------------------------------------------
-- FACTS
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_market_quote (
    quote_id       BIGSERIAL PRIMARY KEY,
    symbol_id      INT NOT NULL REFERENCES dim_symbol(symbol_id),
    source_id      INT NOT NULL REFERENCES dim_source(source_id),
    fetched_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quote_time_utc TIMESTAMPTZ,
    price          NUMERIC(18,6),
    open           NUMERIC(18,6),
    high           NUMERIC(18,6),
    low            NUMERIC(18,6),
    previous_close NUMERIC(18,6),
    change         NUMERIC(18,6),
    change_pct     NUMERIC(10,4),
    raw_payload    JSONB,
    CONSTRAINT uq_fact_quote UNIQUE (symbol_id, source_id, quote_time_utc)
);

CREATE TABLE IF NOT EXISTS fact_market_timeseries (
    timeseries_id   BIGSERIAL PRIMARY KEY,
    symbol_id       INT NOT NULL REFERENCES dim_symbol(symbol_id),
    source_id       INT NOT NULL REFERENCES dim_source(source_id),
    interval_id     INT NOT NULL REFERENCES dim_interval(interval_id),
    candle_time_utc TIMESTAMPTZ NOT NULL,
    open            NUMERIC(18,6),
    high            NUMERIC(18,6),
    low             NUMERIC(18,6),
    close           NUMERIC(18,6),
    volume          NUMERIC(20,2),
    raw_payload     JSONB,
    CONSTRAINT uq_fact_timeseries UNIQUE (symbol_id, interval_id, candle_time_utc)
);

CREATE TABLE IF NOT EXISTS fact_market_indicator (
    indicator_fact_id BIGSERIAL PRIMARY KEY,
    symbol_id         INT NOT NULL REFERENCES dim_symbol(symbol_id),
    source_id         INT NOT NULL REFERENCES dim_source(source_id),
    indicator_id      INT NOT NULL REFERENCES dim_indicator(indicator_id),
    interval_id       INT REFERENCES dim_interval(interval_id),
    candle_time_utc   TIMESTAMPTZ NOT NULL,
    value             NUMERIC(18,6),
    macd              NUMERIC(18,6),
    macd_signal       NUMERIC(18,6),
    macd_hist         NUMERIC(18,6),
    raw_payload       JSONB,
    CONSTRAINT uq_fact_indicator UNIQUE (symbol_id, indicator_id, interval_id, candle_time_utc)
);

-- One row per symbol per fetch. The unique constraint on the day keeps
-- this table from growing by one row every 30 minutes forever.
CREATE TABLE IF NOT EXISTS fact_company_fundamental (
    fundamental_id    BIGSERIAL PRIMARY KEY,
    symbol_id         INT NOT NULL REFERENCES dim_symbol(symbol_id),
    source_id         INT NOT NULL REFERENCES dim_source(source_id),
    fetched_at_utc    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fetched_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    ipo_date          DATE,
    market_cap        NUMERIC(22,2),
    share_outstanding NUMERIC(18,2),
    pe_ratio          NUMERIC(12,4),
    eps_ttm           NUMERIC(12,4),
    gross_margin      NUMERIC(10,4),
    net_margin        NUMERIC(10,4),
    roe               NUMERIC(10,4),
    debt_to_equity    NUMERIC(10,4),
    current_ratio     NUMERIC(10,4),
    beta              NUMERIC(10,4),
    week_52_high      NUMERIC(18,6),
    week_52_low       NUMERIC(18,6),
    raw_profile       JSONB,
    raw_metrics       JSONB,
    CONSTRAINT uq_fact_fundamental UNIQUE (symbol_id, source_id, fetched_date)
);

CREATE TABLE IF NOT EXISTS fact_earnings_calendar (
    earnings_id      BIGSERIAL PRIMARY KEY,
    symbol_id        INT NOT NULL REFERENCES dim_symbol(symbol_id),
    source_id        INT NOT NULL REFERENCES dim_source(source_id),
    fetched_at_utc   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    report_date      DATE,
    hour             TEXT,
    eps_estimate     NUMERIC(12,4),
    eps_actual       NUMERIC(12,4),
    revenue_estimate NUMERIC(22,2),
    revenue_actual   NUMERIC(22,2),
    raw_payload      JSONB,
    CONSTRAINT uq_fact_earnings UNIQUE (symbol_id, report_date)
);

-- ---------------------------------------------------------------------
-- LOGS
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_api_call (
    log_id        BIGSERIAL PRIMARY KEY,
    run_id        BIGINT,
    source_id     INT REFERENCES dim_source(source_id),
    symbol_id     INT REFERENCES dim_symbol(symbol_id),
    called_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    endpoint      TEXT,
    http_status   INT,
    response_ms   INT,
    error_msg     TEXT
);

CREATE TABLE IF NOT EXISTS log_pipeline_run (
    run_id           BIGSERIAL PRIMARY KEY,
    run_uid          TEXT UNIQUE,
    trigger_source   TEXT,
    started_at_utc   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at_utc  TIMESTAMPTZ,
    duration_seconds NUMERIC(10,2),
    status           TEXT NOT NULL DEFAULT 'running',
    symbols_total    INT DEFAULT 0,
    symbols_ok       INT DEFAULT 0,
    symbols_failed   INT DEFAULT 0,
    rows_written     INT DEFAULT 0,
    error_msg        TEXT
);

-- Older installs may predate these columns; add them without failing.
ALTER TABLE log_api_call            ADD COLUMN IF NOT EXISTS run_id BIGINT;
ALTER TABLE fact_company_fundamental ADD COLUMN IF NOT EXISTS fetched_date DATE DEFAULT CURRENT_DATE;

-- CURRENT_DATE is the session's zone, which is the server's (UTC on
-- Render) unless the client asked for another at startup. That made the
-- uniqueness key of this table -- (symbol_id, source_id, fetched_date) --
-- roll over at 01:00 or 02:00 Berlin instead of at midnight, so a run
-- just before midnight and one just after wrote the same "day".
-- local_today() does not depend on the session at all.
ALTER TABLE fact_company_fundamental
    ALTER COLUMN fetched_date SET DEFAULT local_today();

-- ---------------------------------------------------------------------
-- INDEXES
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_ts_symbol_time     ON fact_market_timeseries (symbol_id, interval_id, candle_time_utc DESC);
CREATE INDEX IF NOT EXISTS ix_ts_time            ON fact_market_timeseries (candle_time_utc DESC);
CREATE INDEX IF NOT EXISTS ix_quote_symbol_time  ON fact_market_quote (symbol_id, fetched_at_utc DESC);
CREATE INDEX IF NOT EXISTS ix_ind_symbol_time    ON fact_market_indicator (symbol_id, indicator_id, candle_time_utc DESC);
CREATE INDEX IF NOT EXISTS ix_fund_symbol_time   ON fact_company_fundamental (symbol_id, fetched_at_utc DESC);
CREATE INDEX IF NOT EXISTS ix_earn_report_date   ON fact_earnings_calendar (report_date);
CREATE INDEX IF NOT EXISTS ix_log_called         ON log_api_call (called_at_utc DESC);
CREATE INDEX IF NOT EXISTS ix_log_source_status  ON log_api_call (source_id, http_status, called_at_utc DESC);
CREATE INDEX IF NOT EXISTS ix_run_started        ON log_pipeline_run (started_at_utc DESC);

-- ---------------------------------------------------------------------
-- SEED
-- ---------------------------------------------------------------------
INSERT INTO dim_source (source_name, base_url, notes) VALUES
    ('finnhub',      'https://finnhub.io/api/v1',         '60 req/min'),
    ('alphavantage', 'https://www.alphavantage.co/query', '25 req/day'),
    ('twelvedata',   'https://api.twelvedata.com',        '800 req/day')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO dim_indicator (indicator_name, description, category) VALUES
    ('RSI',  'RSI (14)', 'momentum'),
    ('MACD', 'MACD',     'trend'),
    ('EMA',  'EMA (20)', 'trend'),
    ('SMA',  'SMA (50)', 'trend')
ON CONFLICT (indicator_name) DO NOTHING;

-- One row per interval, under ONE code. 'daily' used to be seeded here
-- beside '1day' because Alpha Vantage's API calls it that, and the
-- collector wrote whatever string it had sent to the API straight into
-- this table. The result was one interval living as two rows with two
-- ids: the daily candles on '1day', the daily indicators on 'daily'.
--
-- Nothing ever failed, which is how it lasted. A join of price to
-- indicator on symbol and date returns every row; the same join with
-- interval_id added returns none, silently -- an empty Power BI visual
-- that reads as "nothing collected yet".
--
-- The collector now maps its provider's word to the code below, so the
-- API's vocabulary stops at the API. Removing the seed matters as much
-- as that fix: this file is re-applied by the migrate action, and while
-- the row was seeded here every migrate put the alias straight back.
--
-- sql/08_merge_duplicate_intervals.sql cleans up a database that still
-- has the split.
INSERT INTO dim_interval (interval_code, interval_type) VALUES
    ('1min','intraday'),
    ('5min','intraday'),
    ('1day','daily')
ON CONFLICT (interval_code) DO NOTHING;
