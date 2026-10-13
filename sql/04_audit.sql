-- =====================================================================
-- 04_audit.sql — data-quality audit of a live database
--
-- Row counts answer "did anything arrive". They do not answer "is what
-- arrived correct", which is the question that matters: a table full of
-- rows whose company_name is NULL loads fine and ruins every Power BI
-- slicer, and a duplicated grain silently doubles every measure.
--
-- Read-only. Safe to run against production at any time.
--
--   psql "$DATABASE_URL" -f sql/04_audit.sql
--
-- Every check emits: a short name, a verdict (OK / WARN / FAIL) and the
-- detail behind it, so the result can be scanned in one pass.
-- =====================================================================

\pset pager off
\pset border 2

WITH checks AS (

    -- ---------------------------------------------------------------
    -- 1. Is anything here at all?
    -- ---------------------------------------------------------------
    SELECT 1 AS ord, 'symbols tracked' AS check_name,
           CASE WHEN COUNT(*) = 0 THEN 'FAIL' ELSE 'OK' END AS verdict,
           COUNT(*)::text || ' symbol(s)' AS detail
      FROM dim_symbol

    UNION ALL
    SELECT 2, 'daily candles',
           CASE WHEN COUNT(*) = 0 THEN 'FAIL'
                WHEN COUNT(*) < 100 THEN 'WARN'
                ELSE 'OK' END,
           COUNT(*)::text || ' row(s)'
      FROM fact_market_timeseries t
      JOIN dim_interval i USING (interval_id)
     WHERE i.interval_code IN ('1day', 'daily')

    -- ---------------------------------------------------------------
    -- 2. Freshness. A pipeline that stopped looks healthy by row count.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 3, 'quote freshness',
           CASE WHEN MAX(fetched_at_utc) IS NULL THEN 'FAIL'
                WHEN MAX(fetched_at_utc) < NOW() - INTERVAL '24 hours' THEN 'FAIL'
                WHEN MAX(fetched_at_utc) < NOW() - INTERVAL '90 minutes' THEN 'WARN'
                ELSE 'OK' END,
           COALESCE(
             'newest quote ' ||
             ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(fetched_at_utc))) / 60)::text ||
             ' min old', 'no quotes at all')
      FROM fact_market_quote

    UNION ALL
    SELECT 4, 'candle freshness',
           CASE WHEN MAX(candle_time_utc) IS NULL THEN 'FAIL'
                WHEN MAX(candle_time_utc) < NOW() - INTERVAL '5 days' THEN 'WARN'
                ELSE 'OK' END,
           COALESCE('newest candle ' || MAX(candle_time_utc)::date::text, 'none')
      FROM fact_market_timeseries

    -- ---------------------------------------------------------------
    -- 3. Symbol profiles. This is the bug that emptied the slicers.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 5, 'symbol names',
           CASE WHEN COUNT(*) FILTER (WHERE company_name IS NULL) > 0
                THEN 'FAIL' ELSE 'OK' END,
           COUNT(*) FILTER (WHERE company_name IS NOT NULL)::text ||
           '/' || COUNT(*)::text || ' named'
      FROM dim_symbol

    UNION ALL
    SELECT 6, 'symbol sectors',
           CASE WHEN COUNT(*) FILTER (WHERE sector IS NULL) > 0
                THEN 'WARN' ELSE 'OK' END,
           COUNT(*) FILTER (WHERE sector IS NOT NULL)::text ||
           '/' || COUNT(*)::text || ' with a sector'
      FROM dim_symbol

    -- ---------------------------------------------------------------
    -- 4. Grain. A duplicate here doubles every measure in the report.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 7, 'price grain',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'FAIL' END,
           CASE WHEN COUNT(*) = 0 THEN 'one row per symbol and day'
                ELSE COUNT(*)::text || ' duplicate (symbol, day) pair(s)' END
      FROM (SELECT symbol_id, date_key
              FROM bi_fact_price_daily
             GROUP BY 1, 2 HAVING COUNT(*) > 1) d

    UNION ALL
    SELECT 8, 'latest-quote grain',
           CASE WHEN COUNT(*) = (SELECT COUNT(*) FROM dim_symbol)
                THEN 'OK' ELSE 'FAIL' END,
           COUNT(*)::text || ' row(s) for ' ||
           (SELECT COUNT(*) FROM dim_symbol)::text || ' symbol(s)'
      FROM bi_fact_quote_latest

    UNION ALL
    SELECT 9, 'indicator grain',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'FAIL' END,
           CASE WHEN COUNT(*) = 0 THEN 'one row per symbol, day and interval'
                ELSE COUNT(*)::text || ' duplicate key(s)' END
      FROM (SELECT symbol_id, date_key, interval_id
              FROM bi_fact_indicator_daily
             GROUP BY 1, 2, 3 HAVING COUNT(*) > 1) d

    -- ---------------------------------------------------------------
    -- 5. Referential integrity. An orphan silently drops from visuals.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 10, 'date keys join',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'FAIL' END,
           COUNT(*)::text || ' price row(s) the calendar cannot match'
      FROM bi_fact_price_daily f
     WHERE NOT EXISTS (SELECT 1 FROM bi_dim_date d WHERE d.date_key = f.date_key)

    UNION ALL
    SELECT 11, 'symbol keys join',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'FAIL' END,
           COUNT(*)::text || ' price row(s) with an unknown symbol'
      FROM bi_fact_price_daily f
     WHERE NOT EXISTS (SELECT 1 FROM bi_dim_symbol s WHERE s.symbol_id = f.symbol_id)

    -- ---------------------------------------------------------------
    -- 6. Category columns. A NULL shows as a blank filter entry.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 12, 'slicer columns',
           CASE WHEN SUM(n) = 0 THEN 'OK' ELSE 'FAIL' END,
           SUM(n)::text || ' NULL(s) across the category columns'
      FROM (
        SELECT COUNT(*) FILTER (WHERE market_cap_band IS NULL
                                   OR valuation_band IS NULL
                                   OR volatility_band IS NULL
                                   OR sector IS NULL
                                   OR exchange IS NULL) AS n
          FROM bi_dim_symbol
        UNION ALL
        SELECT COUNT(*) FILTER (WHERE move_category IS NULL) FROM bi_fact_price_daily
        UNION ALL
        SELECT COUNT(*) FILTER (WHERE rsi_signal IS NULL
                                   OR macd_signal_category IS NULL
                                   OR trend_category IS NULL)
          FROM bi_fact_indicator_daily
        UNION ALL
        SELECT COUNT(*) FILTER (WHERE status_category IS NULL OR latency_band IS NULL)
          FROM bi_fact_api_call
      ) x

    -- ---------------------------------------------------------------
    -- 7. Derived values. A wrong number loads as happily as a right one.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 13, 'change_pct arithmetic',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'FAIL' END,
           COUNT(*)::text || ' row(s) where change_pct disagrees with the closes'
      FROM bi_fact_price_daily
     WHERE prev_close IS NOT NULL AND prev_close <> 0
       AND ABS(change_pct - ((close - prev_close) / prev_close * 100)) > 0.01

    UNION ALL
    SELECT 14, 'move_category agrees',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'FAIL' END,
           COUNT(*)::text || ' row(s) where the label contradicts the number'
      FROM bi_fact_price_daily
     WHERE prev_close IS NOT NULL
       AND ((change_pct > 0 AND move_category NOT IN ('Up', 'Strong Up'))
         OR (change_pct < 0 AND move_category NOT IN ('Down', 'Strong Down')))

    UNION ALL
    SELECT 15, 'api success flags',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'FAIL' END,
           COUNT(*)::text || ' call(s) counted as both success and failure'
      FROM bi_fact_api_call
     WHERE is_success + is_failure <> 1

    -- ---------------------------------------------------------------
    -- 8. Pipeline health, from its own logs.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 16, 'recent runs',
           CASE WHEN COUNT(*) = 0 THEN 'WARN'
                WHEN COUNT(*) FILTER (WHERE status = 'failed') > COUNT(*) / 2 THEN 'FAIL'
                WHEN COUNT(*) FILTER (WHERE status <> 'success') > 0 THEN 'WARN'
                ELSE 'OK' END,
           COUNT(*)::text || ' run(s) in 48h, ' ||
           COUNT(*) FILTER (WHERE status = 'success')::text || ' clean'
      FROM log_pipeline_run
     WHERE started_at_utc > NOW() - INTERVAL '48 hours'

    UNION ALL
    SELECT 17, 'api success rate',
           CASE WHEN COUNT(*) = 0 THEN 'WARN'
                WHEN 100.0 * COUNT(*) FILTER (WHERE http_status BETWEEN 200 AND 299)
                     / COUNT(*) < 80 THEN 'FAIL'
                WHEN 100.0 * COUNT(*) FILTER (WHERE http_status BETWEEN 200 AND 299)
                     / COUNT(*) < 95 THEN 'WARN'
                ELSE 'OK' END,
           COALESCE(ROUND(100.0 * COUNT(*) FILTER (WHERE http_status BETWEEN 200 AND 299)
                          / NULLIF(COUNT(*), 0), 1)::text, '0') ||
           '% of ' || COUNT(*)::text || ' call(s) in 24h'
      FROM log_api_call
     WHERE called_at_utc > NOW() - INTERVAL '24 hours'

    UNION ALL
    SELECT 18, 'runs are linked',
           CASE WHEN COUNT(*) = 0 THEN 'OK' ELSE 'WARN' END,
           COUNT(*)::text || ' API call(s) with no run_id'
      FROM log_api_call
     WHERE called_at_utc > NOW() - INTERVAL '48 hours' AND run_id IS NULL

    -- ---------------------------------------------------------------
    -- 9. Storage, against the hosting cap.
    -- ---------------------------------------------------------------
    UNION ALL
    SELECT 19, 'database size',
           CASE WHEN pg_database_size(current_database()) > 900 * 1024 * 1024 THEN 'FAIL'
                WHEN pg_database_size(current_database()) > 700 * 1024 * 1024 THEN 'WARN'
                ELSE 'OK' END,
           pg_size_pretty(pg_database_size(current_database())) || ' used'

    UNION ALL
    SELECT 20, 'BI views present',
           CASE WHEN COUNT(*) < 16 THEN 'FAIL' ELSE 'OK' END,
           COUNT(*)::text || ' of 16 bi_* view(s)'
      FROM information_schema.views
     WHERE table_schema = 'public' AND table_name LIKE 'bi\_%'
)
SELECT verdict, check_name, detail
  FROM checks
 ORDER BY ord;
