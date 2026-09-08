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
--
-- Run once:  psql "$DATABASE_URL" -f sql/01_powerbi_star.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- DIMENSION: DATE
-- Covers 2015-01-01 .. today + 2 years (earnings calendar is in the future)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_dim_date AS
SELECT
    TO_CHAR(d, 'YYYYMMDD')::INT                       AS date_key,
    d::DATE                                           AS full_date,
    EXTRACT(YEAR    FROM d)::INT                      AS year,
    EXTRACT(QUARTER FROM d)::INT                      AS quarter,
    'Q' || EXTRACT(QUARTER FROM d)::INT               AS quarter_name,
    EXTRACT(MONTH   FROM d)::INT                      AS month_number,
    TO_CHAR(d, 'Mon')                                 AS month_short,
    TO_CHAR(d, 'Month')                               AS month_name,
    EXTRACT(YEAR FROM d)::INT * 100
        + EXTRACT(MONTH FROM d)::INT                  AS year_month_key,
    TO_CHAR(d, 'YYYY-MM')                             AS year_month_label,
    EXTRACT(WEEK FROM d)::INT                         AS iso_week,
    EXTRACT(ISODOW FROM d)::INT                       AS weekday_number,
    TO_CHAR(d, 'Dy')                                  AS weekday_short,
    (EXTRACT(ISODOW FROM d) <= 5)                     AS is_weekday,
    (d::DATE = CURRENT_DATE)                          AS is_today,
    (d::DATE >= CURRENT_DATE - INTERVAL '30 days'
     AND d::DATE <= CURRENT_DATE)                     AS is_last_30_days
FROM generate_series(
        DATE '2015-01-01',
        (CURRENT_DATE + INTERVAL '2 years')::DATE,
        INTERVAL '1 day'
     ) AS d;


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
        t.candle_time_utc::DATE AS trade_date,
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
    TO_CHAR(t.candle_time_utc, 'YYYYMMDD')::INT AS date_key,
    t.symbol_id,
    t.source_id,
    t.interval_id,
    t.candle_time_utc,
    (t.candle_time_utc AT TIME ZONE 'Europe/Berlin') AS candle_time_local,
    EXTRACT(HOUR FROM t.candle_time_utc)::INT   AS hour_utc,
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
    TO_CHAR(f.candle_time_utc, 'YYYYMMDD')::INT              AS date_key,
    f.symbol_id,
    f.interval_id,
    f.candle_time_utc::DATE                                  AS trade_date,

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
    TO_CHAR(COALESCE(q.quote_time_utc, q.fetched_at_utc), 'YYYYMMDD')::INT AS date_key,
    q.symbol_id,
    q.source_id,
    q.quote_time_utc,
    q.fetched_at_utc,
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
    TO_CHAR(COALESCE(q.quote_time_utc, q.fetched_at_utc), 'YYYYMMDD')::INT AS date_key,
    q.quote_id,
    q.symbol_id,
    q.source_id,
    q.quote_time_utc,
    q.fetched_at_utc,
    q.price, q.open, q.high, q.low, q.previous_close, q.change, q.change_pct
FROM fact_market_quote q;


-- ---------------------------------------------------------------------
-- FACT: FUNDAMENTALS (latest snapshot per symbol)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_fundamental_latest AS
SELECT DISTINCT ON (f.symbol_id)
    TO_CHAR(f.fetched_at_utc, 'YYYYMMDD')::INT AS date_key,
    f.symbol_id,
    f.source_id,
    f.fetched_at_utc,
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
    (e.report_date >= CURRENT_DATE) AS is_upcoming
FROM fact_earnings_calendar e
WHERE e.report_date IS NOT NULL;


-- ---------------------------------------------------------------------
-- FACT: PIPELINE HEALTH (API log) -- powers the monitoring page in Power BI
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bi_fact_api_call AS
SELECT
    TO_CHAR(l.called_at_utc, 'YYYYMMDD')::INT AS date_key,
    l.log_id,
    l.source_id,
    l.symbol_id,
    l.called_at_utc,
    (l.called_at_utc AT TIME ZONE 'Europe/Berlin') AS called_at_local,
    EXTRACT(HOUR FROM l.called_at_utc)::INT   AS hour_utc,
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
    (SELECT MAX(fetched_at_utc) FROM fact_market_quote)                     AS last_quote_at,
    (SELECT MAX(candle_time_utc) FROM fact_market_timeseries)               AS last_candle_at,
    (SELECT MAX(called_at_utc) FROM log_api_call)                           AS last_api_call_at,
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
    TO_CHAR(r.started_at_utc, 'YYYYMMDD')::INT AS date_key,
    r.run_id,
    r.run_uid,
    COALESCE(r.trigger_source, 'unknown') AS trigger_source,
    r.started_at_utc,
    r.finished_at_utc,
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
