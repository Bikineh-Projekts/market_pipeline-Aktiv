-- =====================================================================
-- 02_bi_views.sql
-- Power BI ready Star Schema (PostgreSQL)
--
-- Design rules applied here (these matter a lot for Power BI):
--   1. NO "ORDER BY" inside views  -> Power BI ignores it and it kills query folding.
--   2. NO jsonb columns exposed    -> Power BI imports jsonb as unusable "Record" type.
--   3. Every fact has date_key INT (YYYYMMDD) -> single fast relationship to bi_dim_date.
--   4. Explicit column list, no SELECT * -> schema stays stable when tables change.
--   5. Prefix bi_ so BI objects are separate from the raw dim_/fact_/log_ tables.
--   6. Every instant appears TWICE: `<name>_utc` is the absolute instant as
--      stored, and `<name>_local` is the same instant on the Berlin wall
--      clock as a naive timestamp. Power BI has no time-zone type, so a
--      TIMESTAMPTZ arrives converted to whichever zone the report server
--      runs in -- the naive local column is the only one that shows the
--      same clock to everyone. Build visuals on `_local`.
--   7. date_key and every `*_date` column come from the LOCAL day
--      (local_date_key / local_date in 01_schema.sql), not the UTC day. A
--      quote fetched at 23:30 UTC belongs to the next Berlin day, and
--      filing it under the UTC day puts it in the wrong bucket every night.
--
-- Run once:  psql "$DATABASE_URL" -f sql/01_powerbi_star.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- DIMENSION: DATE
--
-- The span is DERIVED from what was actually collected, not fixed.
--
-- It used to be a hardcoded 2015-01-01 .. today + 2 years, and that is the
-- reason a report showed years nobody had data for. A date dimension is
-- generated, not collected, so every year it spans appears in every date
-- slicer whether or not a single price exists in it -- measured on the live
-- database: 9 of the 14 years it produced were completely empty.
--
-- Bounds are snapped OUT to whole calendar years, which is the one thing
-- that must not be trimmed away: Power BI's time intelligence (YTD, YoY,
-- SAMEPERIODLASTYEAR) silently returns wrong numbers on a date table whose
-- first or last year is a partial one. So a part-year of data yields a
-- complete year of calendar, and no more.
--
-- The upper bound also covers the earnings calendar, which is legitimately
-- in the future.
--
-- Deliberately NOT included: fact_company_fundamental.ipo_date. Apple
-- listed in 1980, and letting that set the lower bound would put four
-- empty decades back into the slicer. ipo_date is an attribute of the
-- company, not a date to slice facts by, and it is exposed as one.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_dim_date AS
WITH span AS (
    SELECT
        LEAST(
            COALESCE((SELECT MIN(local_date(candle_time_utc)) FROM fact_market_timeseries), local_today()),
            COALESCE((SELECT MIN(local_date(candle_time_utc)) FROM fact_market_indicator),  local_today()),
            COALESCE((SELECT MIN(local_date(COALESCE(quote_time_utc, fetched_at_utc))) FROM fact_market_quote), local_today()),
            COALESCE((SELECT MIN(report_date) FROM fact_earnings_calendar), local_today()),
            COALESCE((SELECT MIN(local_date(started_at_utc)) FROM log_pipeline_run), local_today()),
            local_today()
        ) AS lo,
        GREATEST(
            COALESCE((SELECT MAX(local_date(candle_time_utc)) FROM fact_market_timeseries), local_today()),
            COALESCE((SELECT MAX(local_date(candle_time_utc)) FROM fact_market_indicator),  local_today()),
            COALESCE((SELECT MAX(local_date(COALESCE(quote_time_utc, fetched_at_utc))) FROM fact_market_quote), local_today()),
            COALESCE((SELECT MAX(report_date) FROM fact_earnings_calendar), local_today()),
            COALESCE((SELECT MAX(local_date(called_at_utc)) FROM log_api_call), local_today()),
            local_today()
        ) AS hi
), bounds AS (
    SELECT DATE_TRUNC('year', lo)::date                                   AS from_date,
           (DATE_TRUNC('year', hi) + INTERVAL '1 year - 1 day')::date     AS to_date
      FROM span
), days AS (
    SELECT generate_series(b.from_date, b.to_date, INTERVAL '1 day')::date AS d
      FROM bounds b
), with_data AS (
    SELECT DISTINCT local_date(candle_time_utc) AS d FROM fact_market_timeseries
)
SELECT
    TO_CHAR(days.d, 'YYYYMMDD')::INT                  AS date_key,
    days.d                                            AS full_date,
    EXTRACT(YEAR    FROM days.d)::INT                 AS year,
    EXTRACT(QUARTER FROM days.d)::INT                 AS quarter,
    'Q' || EXTRACT(QUARTER FROM days.d)::INT          AS quarter_name,
    EXTRACT(MONTH   FROM days.d)::INT                 AS month_number,
    TO_CHAR(days.d, 'Mon')                            AS month_short,
    TO_CHAR(days.d, 'Month')                          AS month_name,
    EXTRACT(YEAR FROM days.d)::INT * 100
        + EXTRACT(MONTH FROM days.d)::INT             AS year_month_key,
    TO_CHAR(days.d, 'YYYY-MM')                        AS year_month_label,
    EXTRACT(WEEK FROM days.d)::INT                    AS iso_week,
    EXTRACT(ISODOW FROM days.d)::INT                  AS weekday_number,
    TO_CHAR(days.d, 'Dy')                             AS weekday_short,
    (EXTRACT(ISODOW FROM days.d) <= 5)                AS is_weekday,
    -- Today in the display zone, not in the session's zone. Between
    -- midnight and 01:00 or 02:00 Berlin those are different days, and
    -- an "is_today" that is wrong for two hours a night is worse than none.
    (days.d = local_today())                          AS is_today,
    (days.d >= local_today() - INTERVAL '30 days'
     AND days.d <= local_today())                     AS is_last_30_days,
    (days.d <= local_today())                         AS is_past_or_today,
    -- Lets a visual drop the days that exist only to complete a year,
    -- without having to narrow the table and break time intelligence.
    (wd.d IS NOT NULL)                                AS has_price_data
FROM days
LEFT JOIN with_data wd ON wd.d = days.d;


-- ---------------------------------------------------------------------
-- DIMENSION: SYMBOL
-- market_cap_band gives you a ready-made slicer category in Power BI.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_dim_symbol AS
WITH latest_fund AS (
    SELECT DISTINCT ON (symbol_id)
           symbol_id, market_cap, beta, pe_ratio
    FROM   fact_company_fundamental
    ORDER  BY symbol_id, fetched_at_utc DESC
)
SELECT
    s.symbol_id,
    s.symbol_code,
    COALESCE(s.company_name, s.symbol_code)            AS company_name,
    COALESCE(s.exchange, 'UNKNOWN')                    AS exchange,
    COALESCE(s.country,  'UNKNOWN')                    AS country,
    COALESCE(s.currency, 'USD')                        AS currency,
    COALESCE(s.sector,   'Uncategorized')              AS sector,
    COALESCE(s.industry, 'Uncategorized')              AS industry,
    f.market_cap,
    CASE
        WHEN f.market_cap IS NULL              THEN 'Unknown'
        WHEN f.market_cap >= 200000            THEN 'Mega Cap'
        WHEN f.market_cap >=  10000            THEN 'Large Cap'
        WHEN f.market_cap >=   2000            THEN 'Mid Cap'
        WHEN f.market_cap >=    300            THEN 'Small Cap'
        ELSE 'Micro Cap'
    END                                                AS market_cap_band,
    CASE
        WHEN f.beta IS NULL     THEN 'Unknown'
        WHEN f.beta >= 1.3      THEN 'High Volatility'
        WHEN f.beta >= 0.8      THEN 'Market Volatility'
        ELSE 'Low Volatility'
    END                                                AS volatility_band,
    CASE
        WHEN f.pe_ratio IS NULL  THEN 'Unknown'
        WHEN f.pe_ratio <  0     THEN 'Negative Earnings'
        WHEN f.pe_ratio < 15     THEN 'Value (P/E < 15)'
        WHEN f.pe_ratio < 30     THEN 'Fair (15-30)'
        ELSE 'Growth / Expensive (30+)'
    END                                                AS valuation_band
FROM dim_symbol s
LEFT JOIN latest_fund f ON f.symbol_id = s.symbol_id;


-- ---------------------------------------------------------------------
-- DIMENSION: SOURCE / INTERVAL / INDICATOR
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_dim_source AS
SELECT
    source_id,
    source_name,
    INITCAP(source_name)  AS source_label,
    base_url,
    COALESCE(notes, '')   AS rate_limit_note
FROM dim_source;

CREATE OR REPLACE VIEW bi_dim_interval AS
SELECT
    interval_id,
    interval_code,
    COALESCE(interval_type, 'unknown') AS interval_type,
    CASE interval_code
        WHEN '1min'  THEN 1
        WHEN '5min'  THEN 2
        WHEN '1day'  THEN 3
        WHEN 'daily' THEN 4
        ELSE 99
    END AS interval_sort_order
FROM dim_interval;

CREATE OR REPLACE VIEW bi_dim_indicator AS
SELECT
    indicator_id,
    indicator_name,
    COALESCE(description, indicator_name) AS description,
    COALESCE(category, 'other')           AS category
FROM dim_indicator;


-- ---------------------------------------------------------------------
-- FACT: DAILY PRICE (OHLCV)  --  the main fact table of the model
-- Includes pre-computed returns so you do not need heavy DAX.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_price_daily AS
WITH daily AS (
    SELECT
        t.symbol_id,
        t.source_id,
        t.interval_id,
        local_date(t.candle_time_utc) AS trade_date,
        t.open, t.high, t.low, t.close, t.volume
    FROM fact_market_timeseries t
    JOIN dim_interval i ON i.interval_id = t.interval_id
    WHERE i.interval_code IN ('1day', 'daily')
),
with_lag AS (
    SELECT
        d.*,
        LAG(d.close) OVER (PARTITION BY d.symbol_id ORDER BY d.trade_date) AS prev_close,
        AVG(d.close) OVER (PARTITION BY d.symbol_id ORDER BY d.trade_date
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)      AS ma_20,
        AVG(d.volume) OVER (PARTITION BY d.symbol_id ORDER BY d.trade_date
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)      AS avg_volume_20
    FROM daily d
)
SELECT
    TO_CHAR(trade_date, 'YYYYMMDD')::INT                    AS date_key,
    symbol_id,
    source_id,
    interval_id,
    trade_date,
    open, high, low, close, volume,
    prev_close,
    (close - prev_close)                                    AS change_abs,
    ROUND(((close - prev_close) / NULLIF(prev_close, 0) * 100)::numeric, 4)
                                                            AS change_pct,
    ROUND(ma_20::numeric, 4)                                AS moving_avg_20,
    ROUND(avg_volume_20::numeric, 2)                        AS avg_volume_20,
    ROUND(((high - low) / NULLIF(low, 0) * 100)::numeric, 4) AS intraday_range_pct,
    CASE
        WHEN prev_close IS NULL              THEN 'No Data'
        WHEN close > prev_close * 1.02       THEN 'Strong Up'
        WHEN close > prev_close              THEN 'Up'
        WHEN close < prev_close * 0.98       THEN 'Strong Down'
        WHEN close < prev_close              THEN 'Down'
        ELSE 'Flat'
    END                                                     AS move_category,
    CASE WHEN volume > avg_volume_20 * 1.5 THEN TRUE ELSE FALSE END
                                                            AS is_volume_spike
FROM with_lag;


-- ---------------------------------------------------------------------
-- FACT: INTRADAY PRICE (1min / 5min)  -- keep separate, it is high volume
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_price_intraday AS
SELECT
    local_date_key(t.candle_time_utc)           AS date_key,
    t.symbol_id,
    t.source_id,
    t.interval_id,
    t.candle_time_utc,
    local_ts(t.candle_time_utc)                 AS candle_time_local,
    local_date(t.candle_time_utc)               AS trade_date,
    EXTRACT(HOUR FROM local_ts(t.candle_time_utc))::INT AS hour_local,
    -- AT TIME ZONE 'UTC' is not redundant: EXTRACT on a TIMESTAMPTZ
    -- resolves in the SESSION's zone, so with the session set to Berlin
    -- this returned the Berlin hour under a column called hour_utc.
    EXTRACT(HOUR FROM (t.candle_time_utc AT TIME ZONE 'UTC'))::INT AS hour_utc,
    t.open, t.high, t.low, t.close, t.volume
FROM fact_market_timeseries t
JOIN dim_interval i ON i.interval_id = t.interval_id
WHERE i.interval_code IN ('1min', '5min');


-- ---------------------------------------------------------------------
-- FACT: INDICATORS -- PIVOTED WIDE
-- This is the single biggest Power BI win: instead of one long/narrow
-- table where RSI/MACD/EMA/SMA are rows, you get one row per
-- (symbol, date, interval) with every indicator as its own column.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_indicator_daily AS
SELECT
    local_date_key(f.candle_time_utc)                        AS date_key,
    f.symbol_id,
    f.interval_id,
    local_date(f.candle_time_utc)                            AS trade_date,

    MAX(CASE WHEN i.indicator_name = 'RSI'  THEN f.value END)       AS rsi_14,
    MAX(CASE WHEN i.indicator_name = 'EMA'  THEN f.value END)       AS ema_20,
    MAX(CASE WHEN i.indicator_name = 'SMA'  THEN f.value END)       AS sma_50,
    MAX(CASE WHEN i.indicator_name = 'MACD' THEN f.macd END)        AS macd,
    MAX(CASE WHEN i.indicator_name = 'MACD' THEN f.macd_signal END) AS macd_signal,
    MAX(CASE WHEN i.indicator_name = 'MACD' THEN f.macd_hist END)   AS macd_hist,

    -- ready-to-slice categories
    CASE
        WHEN MAX(CASE WHEN i.indicator_name='RSI' THEN f.value END) IS NULL THEN 'No Data'
        WHEN MAX(CASE WHEN i.indicator_name='RSI' THEN f.value END) >= 70   THEN 'Overbought'
        WHEN MAX(CASE WHEN i.indicator_name='RSI' THEN f.value END) <= 30   THEN 'Oversold'
        ELSE 'Neutral'
    END AS rsi_signal,

    CASE
        WHEN MAX(CASE WHEN i.indicator_name='MACD' THEN f.macd END) IS NULL THEN 'No Data'
        WHEN MAX(CASE WHEN i.indicator_name='MACD' THEN f.macd END)
           > MAX(CASE WHEN i.indicator_name='MACD' THEN f.macd_signal END)  THEN 'Bullish'
        WHEN MAX(CASE WHEN i.indicator_name='MACD' THEN f.macd END)
           < MAX(CASE WHEN i.indicator_name='MACD' THEN f.macd_signal END)  THEN 'Bearish'
        ELSE 'Neutral'
    END AS macd_signal_category,

    CASE
        WHEN MAX(CASE WHEN i.indicator_name='EMA' THEN f.value END) IS NULL
          OR MAX(CASE WHEN i.indicator_name='SMA' THEN f.value END) IS NULL THEN 'No Data'
        WHEN MAX(CASE WHEN i.indicator_name='EMA' THEN f.value END)
           > MAX(CASE WHEN i.indicator_name='SMA' THEN f.value END)         THEN 'Golden Cross Zone'
        ELSE 'Death Cross Zone'
    END AS trend_category
FROM fact_market_indicator f
JOIN dim_indicator i ON i.indicator_id = f.indicator_id
GROUP BY 1, 2, 3, 4;


-- ---------------------------------------------------------------------
-- FACT: LATEST QUOTE (one row per symbol -- for KPI cards)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_quote_latest AS
SELECT DISTINCT ON (q.symbol_id)
    local_date_key(COALESCE(q.quote_time_utc, q.fetched_at_utc)) AS date_key,
    q.symbol_id,
    q.source_id,
    q.quote_time_utc,
    q.fetched_at_utc,
    local_ts(q.quote_time_utc)   AS quote_time_local,
    local_ts(q.fetched_at_utc)   AS fetched_at_local,
    local_date(COALESCE(q.quote_time_utc, q.fetched_at_utc)) AS quote_date,
    q.price,
    q.open, q.high, q.low,
    q.previous_close,
    q.change,
    q.change_pct,
    CASE
        WHEN q.change_pct IS NULL   THEN 'No Data'
        WHEN q.change_pct >=  2     THEN 'Strong Up'
        WHEN q.change_pct >   0     THEN 'Up'
        WHEN q.change_pct <= -2     THEN 'Strong Down'
        WHEN q.change_pct <   0     THEN 'Down'
        ELSE 'Flat'
    END AS move_category,
    EXTRACT(EPOCH FROM (NOW() - q.fetched_at_utc)) / 60 AS minutes_since_update
FROM fact_market_quote q
ORDER BY q.symbol_id, q.fetched_at_utc DESC;


-- Full quote history (for time-series visuals)
CREATE OR REPLACE VIEW bi_fact_quote_history AS
SELECT
    local_date_key(COALESCE(q.quote_time_utc, q.fetched_at_utc)) AS date_key,
    q.quote_id,
    q.symbol_id,
    q.source_id,
    q.quote_time_utc,
    q.fetched_at_utc,
    local_ts(q.quote_time_utc)   AS quote_time_local,
    local_ts(q.fetched_at_utc)   AS fetched_at_local,
    local_date(COALESCE(q.quote_time_utc, q.fetched_at_utc)) AS quote_date,
    q.price, q.open, q.high, q.low, q.previous_close, q.change, q.change_pct
FROM fact_market_quote q;


-- ---------------------------------------------------------------------
-- FACT: FUNDAMENTALS (latest snapshot per symbol)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_fundamental_latest AS
SELECT DISTINCT ON (f.symbol_id)
    local_date_key(f.fetched_at_utc) AS date_key,
    f.symbol_id,
    f.source_id,
    f.fetched_at_utc,
    local_ts(f.fetched_at_utc)       AS fetched_at_local,
    local_date(f.fetched_at_utc)     AS fetched_date,
    -- An attribute of the company, not a date to slice by: Apple listed in
    -- 1980 and bi_dim_date deliberately does not stretch back to cover it.
    f.ipo_date,
    f.market_cap,
    f.share_outstanding,
    f.pe_ratio,
    f.eps_ttm,
    f.gross_margin,
    f.net_margin,
    f.roe,
    f.debt_to_equity,
    f.current_ratio,
    f.beta,
    f.week_52_high,
    f.week_52_low,
    CASE
        WHEN f.net_margin IS NULL THEN 'Unknown'
        WHEN f.net_margin >= 20   THEN 'High Margin'
        WHEN f.net_margin >= 10   THEN 'Medium Margin'
        WHEN f.net_margin >   0   THEN 'Low Margin'
        ELSE 'Loss Making'
    END AS profitability_band,
    CASE
        WHEN f.debt_to_equity IS NULL THEN 'Unknown'
        WHEN f.debt_to_equity >= 200  THEN 'High Leverage'
        WHEN f.debt_to_equity >=  50  THEN 'Moderate Leverage'
        ELSE 'Low Leverage'
    END AS leverage_band
FROM fact_company_fundamental f
ORDER BY f.symbol_id, f.fetched_at_utc DESC;


-- ---------------------------------------------------------------------
-- FACT: EARNINGS
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_earnings AS
SELECT
    TO_CHAR(e.report_date, 'YYYYMMDD')::INT AS date_key,
    e.earnings_id,
    e.symbol_id,
    e.source_id,
    e.report_date,
    COALESCE(e.hour, 'unknown')  AS report_hour,
    e.eps_estimate,
    e.eps_actual,
    e.revenue_estimate,
    e.revenue_actual,
    ROUND(((e.eps_actual - e.eps_estimate)
           / NULLIF(ABS(e.eps_estimate), 0) * 100)::numeric, 2) AS eps_surprise_pct,
    CASE
        WHEN e.eps_actual IS NULL                THEN 'Upcoming'
        WHEN e.eps_actual >  e.eps_estimate      THEN 'Beat'
        WHEN e.eps_actual <  e.eps_estimate      THEN 'Miss'
        ELSE 'In Line'
    END AS earnings_result,
    (e.report_date >= local_today()) AS is_upcoming,
    (e.report_date - local_today())  AS days_until_report
FROM fact_earnings_calendar e
WHERE e.report_date IS NOT NULL;


-- ---------------------------------------------------------------------
-- FACT: PIPELINE HEALTH (API log) -- powers the monitoring page in Power BI
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_api_call AS
SELECT
    local_date_key(l.called_at_utc)           AS date_key,
    l.log_id,
    l.source_id,
    l.symbol_id,
    l.called_at_utc,
    local_ts(l.called_at_utc)                 AS called_at_local,
    local_date(l.called_at_utc)               AS called_date,
    EXTRACT(HOUR FROM local_ts(l.called_at_utc))::INT AS hour_local,
    -- See the note on hour_utc in bi_fact_price_intraday: the session's
    -- zone, not UTC, is what EXTRACT uses on a TIMESTAMPTZ.
    EXTRACT(HOUR FROM (l.called_at_utc AT TIME ZONE 'UTC'))::INT AS hour_utc,
    COALESCE(l.endpoint, 'unknown')           AS endpoint,
    l.http_status,
    l.response_ms,
    COALESCE(l.error_msg, '')                 AS error_msg,
    CASE WHEN l.http_status BETWEEN 200 AND 299 THEN 1 ELSE 0 END AS is_success,
    CASE WHEN l.http_status BETWEEN 200 AND 299 THEN 0 ELSE 1 END AS is_failure,
    CASE
        WHEN l.http_status IS NULL                   THEN 'No Response'
        WHEN l.http_status BETWEEN 200 AND 299       THEN 'Success'
        WHEN l.http_status IN (429)                  THEN 'Rate Limited'
        WHEN l.http_status BETWEEN 400 AND 499       THEN 'Client Error'
        WHEN l.http_status >= 500                    THEN 'Server Error'
        ELSE 'Other'
    END AS status_category,
    CASE
        WHEN l.response_ms IS NULL   THEN 'Unknown'
        WHEN l.response_ms <  300    THEN 'Fast (<300ms)'
        WHEN l.response_ms < 1000    THEN 'Normal (300-1000ms)'
        WHEN l.response_ms < 3000    THEN 'Slow (1-3s)'
        ELSE 'Very Slow (3s+)'
    END AS latency_band
FROM log_api_call l;


-- ---------------------------------------------------------------------
-- SUMMARY: one row = current health of the whole pipeline.
-- Used by the JS monitor AND by the Power BI header cards.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_pipeline_health AS
SELECT
    (SELECT COUNT(*) FROM dim_symbol)                                       AS symbols_tracked,
    -- The clock this whole model reports in, carried as data so the
    -- dashboard and any report can label a timestamp without hardcoding it.
    app_timezone()                                                          AS display_timezone,
    local_ts(NOW())                                                         AS as_of_local,
    NOW()                                                                   AS as_of_utc,
    (SELECT MAX(fetched_at_utc) FROM fact_market_quote)                     AS last_quote_at,
    local_ts((SELECT MAX(fetched_at_utc) FROM fact_market_quote))           AS last_quote_at_local,
    (SELECT MAX(candle_time_utc) FROM fact_market_timeseries)               AS last_candle_at,
    local_ts((SELECT MAX(candle_time_utc) FROM fact_market_timeseries))     AS last_candle_at_local,
    (SELECT MAX(called_at_utc) FROM log_api_call)                           AS last_api_call_at,
    local_ts((SELECT MAX(called_at_utc) FROM log_api_call))                 AS last_api_call_at_local,
    EXTRACT(EPOCH FROM (NOW() - (SELECT MAX(fetched_at_utc)
                                 FROM fact_market_quote))) / 60             AS quote_age_minutes,
    (SELECT COUNT(*) FROM log_api_call
      WHERE called_at_utc > NOW() - INTERVAL '24 hours')                    AS api_calls_24h,
    (SELECT COUNT(*) FROM log_api_call
      WHERE called_at_utc > NOW() - INTERVAL '24 hours'
        AND (http_status IS NULL OR http_status NOT BETWEEN 200 AND 299))   AS api_errors_24h,
    (SELECT ROUND(AVG(response_ms)::numeric, 0) FROM log_api_call
      WHERE called_at_utc > NOW() - INTERVAL '24 hours')                    AS avg_latency_ms_24h,
    (SELECT ROUND(
        100.0 * COUNT(*) FILTER (WHERE http_status BETWEEN 200 AND 299)
        / NULLIF(COUNT(*), 0), 2)
     FROM log_api_call
     WHERE called_at_utc > NOW() - INTERVAL '24 hours')                     AS success_rate_24h,
    (SELECT COUNT(*) FROM fact_market_timeseries)                           AS total_candles,
    (SELECT COUNT(*) FROM fact_market_indicator)                            AS total_indicators;


-- ---------------------------------------------------------------------
-- FACT: PIPELINE RUNS -- one row per ETL execution
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_pipeline_run AS
SELECT
    local_date_key(r.started_at_utc) AS date_key,
    r.run_id,
    r.run_uid,
    COALESCE(r.trigger_source, 'unknown') AS trigger_source,
    r.started_at_utc,
    r.finished_at_utc,
    local_ts(r.started_at_utc)       AS started_at_local,
    local_ts(r.finished_at_utc)      AS finished_at_local,
    local_date(r.started_at_utc)     AS run_date,
    r.duration_seconds,
    r.status,
    r.symbols_total,
    r.symbols_ok,
    r.symbols_failed,
    r.rows_written,
    COALESCE(r.error_msg, '') AS error_msg,
    CASE WHEN r.status = 'success' THEN 1 ELSE 0 END AS is_success,
    CASE
        WHEN r.duration_seconds IS NULL THEN 'Unknown'
        WHEN r.duration_seconds <  60   THEN 'Fast (<1min)'
        WHEN r.duration_seconds < 300   THEN 'Normal (1-5min)'
        ELSE 'Slow (5min+)'
    END AS duration_band
FROM log_pipeline_run r;


-- ---------------------------------------------------------------------
-- STORAGE WATCH -- Neon free plan caps a project at 0.5 GB.
-- Check this occasionally so a full disk never surprises you.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_storage_usage AS
SELECT
    c.relname                                   AS table_name,
    pg_total_relation_size(c.oid)               AS bytes,
    ROUND(pg_total_relation_size(c.oid) / 1048576.0, 2) AS megabytes,
    ROUND(100.0 * pg_total_relation_size(c.oid)
          / NULLIF(pg_database_size(current_database()), 0), 1) AS pct_of_database
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'r';
