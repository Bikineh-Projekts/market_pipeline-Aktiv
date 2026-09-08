-- =====================================================================
-- 03_maintenance.sql — keep the database inside the Neon free 0.5 GB cap
--
-- Run monthly via the "Maintenance" workflow. Everything here is safe:
-- it only removes high-volume, low-value rows and reclaims space.
--
-- Tune the intervals below to taste. Defaults are conservative.
-- =====================================================================

\echo '--- size before ---'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;

-- ---------------------------------------------------------------------
-- 1. API call log — pure operational noise after a month.
-- ---------------------------------------------------------------------
DELETE FROM log_api_call
 WHERE called_at_utc < NOW() - INTERVAL '60 days';

-- ---------------------------------------------------------------------
-- 2. Intraday candles — the single biggest consumer of storage.
--    Daily candles are kept forever; only 1min/5min are trimmed.
-- ---------------------------------------------------------------------
DELETE FROM fact_market_timeseries t
 USING dim_interval i
 WHERE i.interval_id = t.interval_id
   AND i.interval_code IN ('1min', '5min')
   AND t.candle_time_utc < NOW() - INTERVAL '30 days';

-- ---------------------------------------------------------------------
-- 3. Real-time quotes — the daily candle table already holds the history,
--    so quotes older than 90 days add nothing.
-- ---------------------------------------------------------------------
DELETE FROM fact_market_quote
 WHERE fetched_at_utc < NOW() - INTERVAL '90 days';

-- ---------------------------------------------------------------------
-- 4. Old run logs.
-- ---------------------------------------------------------------------
DELETE FROM log_pipeline_run
 WHERE started_at_utc < NOW() - INTERVAL '180 days';

-- ---------------------------------------------------------------------
-- 5. Drop raw JSON from old rows. The parsed columns keep all the value;
--    raw_payload only matters while debugging a recent fetch.
-- ---------------------------------------------------------------------
UPDATE fact_market_timeseries
   SET raw_payload = NULL
 WHERE raw_payload IS NOT NULL
   AND candle_time_utc < NOW() - INTERVAL '90 days';

UPDATE fact_market_indicator
   SET raw_payload = NULL
 WHERE raw_payload IS NOT NULL
   AND candle_time_utc < NOW() - INTERVAL '90 days';

-- ---------------------------------------------------------------------
-- 6. Reclaim the space. Neon computes suspend, which loses the activity
--    statistics autovacuum relies on, so a manual VACUUM matters here
--    more than it would on an always-on Postgres.
-- ---------------------------------------------------------------------
VACUUM (ANALYZE) fact_market_timeseries;
VACUUM (ANALYZE) fact_market_indicator;
VACUUM (ANALYZE) fact_market_quote;
VACUUM (ANALYZE) log_api_call;

\echo '--- size after ---'
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;

\echo '--- biggest tables ---'
SELECT table_name, megabytes, pct_of_database
  FROM bi_storage_usage
 ORDER BY bytes DESC
 LIMIT 10;
