-- =====================================================================
-- 05_dates.sql — where, exactly, do the dates in this database come from?
--
-- "I still see 2023 and 2025 dates" has two completely different causes
-- and they need opposite fixes, so this report separates them:
--
--   (a) FACT ROWS with old dates. Real price history. A backfill of 1000
--       daily candles is about four trading years, so it genuinely
--       reaches back past 2022. Nothing is wrong; there is simply more
--       history than expected. The fix is to load less, or to filter.
--
--   (b) CALENDAR ROWS with no facts behind them. bi_dim_date is generated,
--       not collected, so every year it spans appears in every Power BI
--       date slicer whether or not a single price exists in it. This is
--       what makes empty years show up in a report. The fix is to narrow
--       the calendar.
--
-- Read-only.  psql "$DATABASE_URL" -f sql/05_dates.sql
-- =====================================================================

\pset pager off
\pset border 2

\echo ''
\echo '=== 1. Server time zone and clock ==================================='
-- If this says anything other than UTC, every stored timestamp has been
-- interpreted against a different offset and the comparison below lies.
SELECT current_setting('TimeZone')                    AS server_timezone,
       NOW()                                          AS now_as_stored,
       NOW() AT TIME ZONE 'UTC'                       AS now_utc,
       NOW() AT TIME ZONE 'Europe/Berlin'             AS now_berlin,
       TO_CHAR(NOW() AT TIME ZONE 'Europe/Berlin', 'TZ') AS berlin_abbrev,
       EXTRACT(HOUR FROM (NOW() AT TIME ZONE 'Europe/Berlin'))
         - EXTRACT(HOUR FROM (NOW() AT TIME ZONE 'UTC'))  AS berlin_offset_hours;

\echo ''
\echo '=== 2. Date span of every fact table ==============================='
-- The honest answer to "what period is in here".
SELECT 'fact_market_timeseries' AS table_name, 'candle_time_utc' AS column_name,
       COUNT(*) AS rows,
       MIN(candle_time_utc)::date AS earliest,
       MAX(candle_time_utc)::date AS latest
  FROM fact_market_timeseries
UNION ALL
SELECT 'fact_market_indicator', 'candle_time_utc', COUNT(*),
       MIN(candle_time_utc)::date, MAX(candle_time_utc)::date
  FROM fact_market_indicator
UNION ALL
SELECT 'fact_market_quote', 'fetched_at_utc', COUNT(*),
       MIN(fetched_at_utc)::date, MAX(fetched_at_utc)::date
  FROM fact_market_quote
UNION ALL
SELECT 'fact_market_quote', 'quote_time_utc', COUNT(*),
       MIN(quote_time_utc)::date, MAX(quote_time_utc)::date
  FROM fact_market_quote
UNION ALL
SELECT 'fact_earnings_calendar', 'report_date', COUNT(*),
       MIN(report_date), MAX(report_date)
  FROM fact_earnings_calendar
UNION ALL
SELECT 'fact_company_fundamental', 'ipo_date', COUNT(*),
       MIN(ipo_date), MAX(ipo_date)
  FROM fact_company_fundamental
UNION ALL
SELECT 'log_api_call', 'called_at_utc', COUNT(*),
       MIN(called_at_utc)::date, MAX(called_at_utc)::date
  FROM log_api_call
UNION ALL
SELECT 'log_pipeline_run', 'started_at_utc', COUNT(*),
       MIN(started_at_utc)::date, MAX(started_at_utc)::date
  FROM log_pipeline_run
 ORDER BY 1, 2;

\echo ''
\echo '=== 3. Candles per year and interval (cause (a)) ===================='
-- This is the table that answers "are the 2023 rows real prices?".
SELECT EXTRACT(YEAR FROM local_ts(t.candle_time_utc))::int AS year,
       i.interval_code,
       COUNT(*)                                  AS candles,
       COUNT(DISTINCT t.symbol_id)               AS symbols,
       MIN(t.candle_time_utc)::date              AS first_day,
       MAX(t.candle_time_utc)::date              AS last_day
  FROM fact_market_timeseries t
  JOIN dim_interval i USING (interval_id)
 GROUP BY 1, 2
 ORDER BY 1 DESC, 2;

\echo ''
\echo '=== 4. Indicators per year ========================================='
SELECT EXTRACT(YEAR FROM local_ts(candle_time_utc))::int AS year,
       COUNT(*) AS rows,
       MIN(candle_time_utc)::date AS first_day,
       MAX(candle_time_utc)::date AS last_day
  FROM fact_market_indicator
 GROUP BY 1
 ORDER BY 1 DESC;

\echo ''
\echo '=== 5. Earnings report dates ======================================='
-- Finnhub returns a window around today, so both past and future rows are
-- expected here. Rows years away are not.
SELECT e.report_date,
       s.symbol_code,
       (e.report_date >= CURRENT_DATE) AS is_future,
       e.eps_estimate,
       e.eps_actual
  FROM fact_earnings_calendar e
  JOIN dim_symbol s USING (symbol_id)
 ORDER BY e.report_date DESC
 LIMIT 40;

\echo ''
\echo '=== 6. The generated calendar vs the facts (cause (b)) ============='
-- The important number is empty_days. Every one of those is a date that
-- appears in a Power BI slicer with nothing behind it.
WITH cal AS (
    SELECT MIN(full_date) AS cal_from, MAX(full_date) AS cal_to, COUNT(*) AS cal_days
      FROM bi_dim_date
), fact AS (
    SELECT MIN(trade_date) AS fact_from, MAX(trade_date) AS fact_to,
           COUNT(DISTINCT trade_date) AS fact_days
      FROM bi_fact_price_daily
)
SELECT cal_from, cal_to, cal_days,
       fact_from, fact_to, fact_days,
       cal_days - fact_days AS empty_days,
       ROUND(100.0 * (cal_days - fact_days) / NULLIF(cal_days, 0), 1) AS empty_pct
  FROM cal, fact;

\echo ''
\echo '=== 7. Calendar years that contain no price at all ================='
-- Each row here is a year the user sees in a slicer for no reason.
SELECT d.year,
       COUNT(*)                                   AS calendar_days,
       COUNT(f.date_key)                          AS days_with_a_price,
       CASE WHEN COUNT(f.date_key) = 0
            THEN 'EMPTY — shows in slicers with no data'
            ELSE 'has data' END                   AS verdict
  FROM bi_dim_date d
  LEFT JOIN (SELECT DISTINCT date_key FROM bi_fact_price_daily) f
         ON f.date_key = d.date_key
 GROUP BY d.year
 ORDER BY d.year;

\echo ''
\echo '=== 8. Timestamps that cannot be right ============================='
-- A candle in the future, or before the symbol could have traded, means a
-- provider returned something odd or a parse went wrong.
SELECT 'future candles' AS problem, COUNT(*) AS rows
  FROM fact_market_timeseries WHERE candle_time_utc > NOW() + INTERVAL '2 days'
UNION ALL
SELECT 'candles before 1990', COUNT(*)
  FROM fact_market_timeseries WHERE candle_time_utc < TIMESTAMPTZ '1990-01-01'
UNION ALL
SELECT 'quotes in the future', COUNT(*)
  FROM fact_market_quote WHERE COALESCE(quote_time_utc, fetched_at_utc) > NOW() + INTERVAL '1 day'
UNION ALL
SELECT 'candles at exactly midnight UTC', COUNT(*)
  FROM fact_market_timeseries
 WHERE (candle_time_utc AT TIME ZONE 'UTC')::time = TIME '00:00:00'
UNION ALL
SELECT 'daily candles on a weekend', COUNT(*)
  FROM fact_market_timeseries t JOIN dim_interval i USING (interval_id)
 WHERE i.interval_code IN ('1day','daily')
   AND EXTRACT(ISODOW FROM local_ts(t.candle_time_utc)) > 5
 ORDER BY 1;

\echo ''
\echo '=== 9. The same instant, UTC and Berlin ============================'
-- Proof of what the conversion does, on the newest row of each kind.
SELECT 'newest quote' AS row_kind,
       MAX(fetched_at_utc)                                 AS utc,
       MAX(fetched_at_utc) AT TIME ZONE 'Europe/Berlin'     AS berlin
  FROM fact_market_quote
UNION ALL
SELECT 'newest candle', MAX(candle_time_utc),
       MAX(candle_time_utc) AT TIME ZONE 'Europe/Berlin'
  FROM fact_market_timeseries
UNION ALL
SELECT 'newest API call', MAX(called_at_utc),
       MAX(called_at_utc) AT TIME ZONE 'Europe/Berlin'
  FROM log_api_call
UNION ALL
SELECT 'newest run', MAX(started_at_utc),
       MAX(started_at_utc) AT TIME ZONE 'Europe/Berlin'
  FROM log_pipeline_run;
