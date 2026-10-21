-- =====================================================================
--  One look at whether the local copy is actually sound.
--
--  Paste the whole thing into a pgAdmin query window against marketdb
--  and run it. It returns ONE table: every check, what it found, and
--  whether that is OK. It writes nothing and locks nothing, so it is
--  safe to run at any time, including while a pull is happening.
--
--  Read the `status` column. OK means nothing to do. WARN means look,
--  but it is probably explainable. FAIL means something is wrong.
--
--  Why a single result set rather than fifteen queries: so there is one
--  thing to run and one thing to send, and so a check that returns no
--  rows -- which is what "no orphans" looks like -- still prints a line
--  saying it passed. A check that is silent when it passes is a check
--  nobody can tell ran.
-- =====================================================================

-- replication_state is created by tools/replicate.py, not by the schema,
-- so on a database that has only ever been filled from the published
-- export it does not exist and every query below would fail to parse --
-- SQL resolves table names before it runs anything, so one missing table
-- takes the whole report with it. This is the same definition
-- replicate.py uses, and IF NOT EXISTS makes it a no-op everywhere else.
CREATE TABLE IF NOT EXISTS replication_state (
    table_name      TEXT PRIMARY KEY,
    last_source_id  BIGINT NOT NULL DEFAULT 0,
    rows_copied     BIGINT NOT NULL DEFAULT 0,
    last_pass_at    TIMESTAMPTZ,
    source_label    TEXT
);

WITH

-- --- how current is the data ------------------------------------------
freshness AS (
    SELECT
        MAX(candle_time_utc)                                   AS newest,
        EXTRACT(EPOCH FROM (NOW() - MAX(candle_time_utc)))/3600 AS hours_old
    FROM fact_market_timeseries
),

-- The markets are shut at night and at weekends, so "hours behind now"
-- is the wrong measure on a Monday morning. What matters is whether the
-- local copy is behind the last pull, not behind the clock.
last_pull AS (
    SELECT MAX(last_pass_at) AS at,
           EXTRACT(EPOCH FROM (NOW() - MAX(last_pass_at)))/60 AS minutes_ago
    FROM replication_state
),

-- --- referential integrity --------------------------------------------
-- Foreign keys enforce these, so a non-zero count here means something
-- got in around them. Checked anyway: this is the failure that loses a
-- company's prices to another company, and it is silent.
orphans AS (
    SELECT
        (SELECT COUNT(*) FROM fact_market_timeseries f
          LEFT JOIN dim_symbol d USING (symbol_id) WHERE d.symbol_id IS NULL)   AS ts_symbol,
        (SELECT COUNT(*) FROM fact_market_timeseries f
          LEFT JOIN dim_interval d USING (interval_id) WHERE d.interval_id IS NULL) AS ts_interval,
        (SELECT COUNT(*) FROM fact_market_indicator f
          LEFT JOIN dim_indicator d USING (indicator_id) WHERE d.indicator_id IS NULL) AS ind_indicator,
        (SELECT COUNT(*) FROM fact_market_quote f
          LEFT JOIN dim_symbol d USING (symbol_id) WHERE d.symbol_id IS NULL)   AS quote_symbol
),

-- --- duplicates --------------------------------------------------------
-- The UNIQUE constraints should make these impossible. If one ever
-- failed to apply -- an older install, a restore that skipped it -- the
-- rows pile up silently and every average is quietly wrong.
dupes AS (
    SELECT
        (SELECT COUNT(*) FROM (
            SELECT 1 FROM fact_market_timeseries
             GROUP BY symbol_id, interval_id, candle_time_utc HAVING COUNT(*) > 1) x) AS ts,
        (SELECT COUNT(*) FROM (
            SELECT 1 FROM fact_market_indicator
             GROUP BY symbol_id, indicator_id, interval_id, candle_time_utc HAVING COUNT(*) > 1) x) AS ind,
        (SELECT COUNT(*) FROM (
            SELECT 1 FROM fact_market_quote
             GROUP BY symbol_id, source_id, quote_time_utc HAVING COUNT(*) > 1) x) AS quote
),

-- --- values that cannot be true ---------------------------------------
-- A candle whose high is below its low, or whose close sits outside the
-- range, is not a gap in the data -- it is wrong data, and it will not
-- announce itself.
impossible AS (
    SELECT
        COUNT(*) FILTER (WHERE high < low)                        AS high_below_low,
        COUNT(*) FILTER (WHERE close > high OR close < low)       AS close_outside,
        COUNT(*) FILTER (WHERE open  > high OR open  < low)       AS open_outside,
        COUNT(*) FILTER (WHERE open <= 0 OR high <= 0
                            OR low  <= 0 OR close <= 0)           AS non_positive,
        COUNT(*) FILTER (WHERE close IS NULL)                     AS null_close,
        COUNT(*) FILTER (WHERE candle_time_utc > NOW() + INTERVAL '1 day') AS in_the_future
    FROM fact_market_timeseries
),

-- --- the BI layer -------------------------------------------------------
views AS (
    SELECT COUNT(*) AS n FROM information_schema.views
     WHERE table_schema = 'public' AND table_name LIKE 'bi_%'
),

-- --- size ---------------------------------------------------------------
sizes AS (
    SELECT pg_size_pretty(pg_database_size(current_database())) AS db
)

SELECT * FROM (

    SELECT 1 AS n, 'newest candle' AS check,
           COALESCE(to_char((SELECT newest FROM freshness), 'YYYY-MM-DD HH24:MI'), '(none)') AS found,
           CASE WHEN (SELECT newest FROM freshness) IS NULL THEN 'FAIL'
                -- A negative age means the newest candle is dated in the
                -- future. Without this branch that read as "very fresh"
                -- and printed OK over a candle stamped next year, which
                -- is the one thing this line must never do.
                WHEN (SELECT hours_old FROM freshness) < -24 THEN 'FAIL'
                WHEN (SELECT hours_old FROM freshness) < 96 THEN 'OK'
                ELSE 'WARN' END AS status,
           'in the future = bad data; over 4 days behind = only normal over a long holiday' AS note

    UNION ALL SELECT 2, 'last pull ran',
           COALESCE(to_char((SELECT at FROM last_pull), 'YYYY-MM-DD HH24:MI')
                    || '  (' || ROUND((SELECT minutes_ago FROM last_pull)) || ' min ago)', '(never)'),
           CASE WHEN (SELECT at FROM last_pull) IS NULL THEN 'WARN'
                WHEN (SELECT minutes_ago FROM last_pull) < 60 THEN 'OK'
                ELSE 'WARN' END,
           '(never) = replicate.py has not run here; otherwise every 15 min while logged in'

    UNION ALL SELECT 3, 'candles', (SELECT COUNT(*)::text FROM fact_market_timeseries),
           CASE WHEN (SELECT COUNT(*) FROM fact_market_timeseries) > 0 THEN 'OK' ELSE 'FAIL' END, ''
    UNION ALL SELECT 4, 'quotes', (SELECT COUNT(*)::text FROM fact_market_quote), 'OK', ''
    UNION ALL SELECT 5, 'indicators', (SELECT COUNT(*)::text FROM fact_market_indicator), 'OK', ''
    UNION ALL SELECT 6, 'fundamentals', (SELECT COUNT(*)::text FROM fact_company_fundamental), 'OK', ''
    UNION ALL SELECT 7, 'earnings', (SELECT COUNT(*)::text FROM fact_earnings_calendar), 'OK', ''
    UNION ALL SELECT 8, 'pipeline runs', (SELECT COUNT(*)::text FROM log_pipeline_run), 'OK', ''
    UNION ALL SELECT 9, 'api calls', (SELECT COUNT(*)::text FROM log_api_call), 'OK', ''

    UNION ALL SELECT 10, 'symbols',
           (SELECT COUNT(*)::text FROM dim_symbol) || ' (' ||
           (SELECT COUNT(*) FROM dim_symbol WHERE company_name IS NOT NULL) || ' named, ' ||
           (SELECT COUNT(*) FROM dim_symbol WHERE sector IS NOT NULL) || ' with sector)',
           CASE WHEN (SELECT COUNT(*) FROM dim_symbol
                       WHERE company_name IS NULL OR sector IS NULL) = 0
                THEN 'OK' ELSE 'WARN' END,
           'a symbol with no name means the profile call did not land'

    UNION ALL SELECT 11, 'date range covered',
           COALESCE((SELECT to_char(MIN(candle_time_utc), 'YYYY-MM-DD') || ' to '
                          || to_char(MAX(candle_time_utc), 'YYYY-MM-DD')
                       FROM fact_market_timeseries), '(empty)'), 'OK', ''

    UNION ALL SELECT 12, 'orphan rows (wrong company risk)',
           (SELECT (ts_symbol + ts_interval + ind_indicator + quote_symbol)::text FROM orphans),
           CASE WHEN (SELECT ts_symbol + ts_interval + ind_indicator + quote_symbol FROM orphans) = 0
                THEN 'OK' ELSE 'FAIL' END,
           'must be 0: a fact pointing at a missing dimension'

    UNION ALL SELECT 13, 'duplicate rows',
           (SELECT (ts + ind + quote)::text FROM dupes),
           CASE WHEN (SELECT ts + ind + quote FROM dupes) = 0 THEN 'OK' ELSE 'FAIL' END,
           'must be 0: the UNIQUE constraints should prevent these'

    UNION ALL SELECT 14, 'impossible candles',
           (SELECT (high_below_low + close_outside + open_outside
                    + non_positive + null_close + in_the_future)::text FROM impossible),
           CASE WHEN (SELECT high_below_low + close_outside + open_outside
                           + non_positive + null_close + in_the_future FROM impossible) = 0
                THEN 'OK' ELSE 'FAIL' END,
           'high<low, close outside the range, zero prices, future dates'

    UNION ALL SELECT 15, 'BI views for Power BI',
           (SELECT n::text FROM views),
           CASE WHEN (SELECT n FROM views) >= 16 THEN 'OK' ELSE 'WARN' END,
           'expected 16'

    UNION ALL SELECT 16, 'tables being replicated',
           (SELECT COUNT(*)::text FROM replication_state),
           CASE WHEN (SELECT COUNT(*) FROM replication_state) >= 7 THEN 'OK' ELSE 'WARN' END,
           'expected 7; 0 means this copy comes from the published export'

    UNION ALL SELECT 17, 'database size', (SELECT db FROM sizes), 'OK', ''

    UNION ALL SELECT 18, 'no credential stored in replication_state',
           CASE WHEN EXISTS (SELECT 1 FROM replication_state
                              WHERE source_label LIKE '%@%' OR source_label LIKE '%://%')
                THEN 'SOMETHING LOOKS LIKE A URL' ELSE 'clean' END,
           CASE WHEN EXISTS (SELECT 1 FROM replication_state
                              WHERE source_label LIKE '%@%' OR source_label LIKE '%://%')
                THEN 'FAIL' ELSE 'OK' END,
           'this column once held a password'

) checks ORDER BY n;
