-- =====================================================================
--  Merge interval codes that mean the same thing into one.
--
--  Run once per database -- yours and Render's. Safe to run again: on a
--  database that is already clean it changes nothing and says so.
--
--  WHAT WENT WRONG
--  Alpha Vantage calls the daily interval "daily". Twelve Data calls it
--  "1day". Each collector wrote whatever string it sent to its API
--  straight into dim_interval, so one interval ended up as two rows with
--  two different interval_ids: the daily CANDLES under "1day", the daily
--  INDICATORS under "daily".
--
--  Nothing failed, which is why it survived. bi_fact_price_daily had
--  already been taught to accept either code -- the symptom patched in
--  one view, the split left in the data. It only bites on a join:
--
--      price JOIN indicator ON symbol + date                -> every row
--      price JOIN indicator ON symbol + date + interval_id  -> ZERO rows
--
--  with no error. In Power BI that is an empty visual that reads as "no
--  data collected yet" rather than as a broken relationship.
--
--  WHAT THIS DOES
--  Moves every fact row off the alias and onto the canonical code, then
--  deletes the alias from dim_interval. Prices are untouched -- they are
--  already on "1day". It is one transaction: it either all happens or
--  none of it does.
--
--  The UNIQUE constraint is the part that needs care. Moving a row to
--  another interval_id can collide with a row that is already there, and
--  a bare UPDATE would abort the whole migration on the first clash. So
--  rows that would collide are deleted instead of moved: the row they
--  would collide with holds the same measurement for the same symbol,
--  indicator and timestamp, so nothing is lost.
-- =====================================================================

BEGIN;

CREATE TEMP TABLE interval_merge(alias_id INT, canonical_id INT) ON COMMIT DROP;

INSERT INTO interval_merge(alias_id, canonical_id)
SELECT alias.interval_id, canonical.interval_id
  FROM dim_interval alias
  JOIN dim_interval canonical
    ON canonical.interval_code = CASE alias.interval_code
                                     WHEN 'daily'   THEN '1day'
                                     WHEN 'weekly'  THEN '1week'
                                     WHEN 'monthly' THEN '1month'
                                 END
 WHERE alias.interval_code IN ('daily', 'weekly', 'monthly');

\echo '--- aliases found (empty means this database is already clean) ---'
SELECT a.interval_code AS alias, c.interval_code AS canonical,
       (SELECT COUNT(*) FROM fact_market_indicator  f WHERE f.interval_id = m.alias_id) AS indicator_rows,
       (SELECT COUNT(*) FROM fact_market_timeseries f WHERE f.interval_id = m.alias_id) AS candle_rows
  FROM interval_merge m
  JOIN dim_interval a ON a.interval_id = m.alias_id
  JOIN dim_interval c ON c.interval_id = m.canonical_id;

-- --- indicators -------------------------------------------------------
-- UNIQUE (symbol_id, indicator_id, interval_id, candle_time_utc)
DELETE FROM fact_market_indicator f
 USING interval_merge m
 WHERE f.interval_id = m.alias_id
   AND EXISTS (SELECT 1 FROM fact_market_indicator k
                WHERE k.symbol_id       = f.symbol_id
                  AND k.indicator_id    = f.indicator_id
                  AND k.interval_id     = m.canonical_id
                  AND k.candle_time_utc = f.candle_time_utc);

UPDATE fact_market_indicator f
   SET interval_id = m.canonical_id
  FROM interval_merge m
 WHERE f.interval_id = m.alias_id;

-- --- candles ----------------------------------------------------------
-- UNIQUE (symbol_id, interval_id, candle_time_utc). Expected to be a
-- no-op; included so the migration is complete rather than only true of
-- the databases that exist today.
DELETE FROM fact_market_timeseries f
 USING interval_merge m
 WHERE f.interval_id = m.alias_id
   AND EXISTS (SELECT 1 FROM fact_market_timeseries k
                WHERE k.symbol_id       = f.symbol_id
                  AND k.interval_id     = m.canonical_id
                  AND k.candle_time_utc = f.candle_time_utc);

UPDATE fact_market_timeseries f
   SET interval_id = m.canonical_id
  FROM interval_merge m
 WHERE f.interval_id = m.alias_id;

-- --- the alias rows themselves ---------------------------------------
DELETE FROM dim_interval d
 USING interval_merge m
 WHERE d.interval_id = m.alias_id;

\echo '--- dim_interval afterwards ---'
SELECT interval_id, interval_code, interval_type
  FROM dim_interval ORDER BY interval_id;

\echo '--- every interval still in use, and by what ---'
SELECT i.interval_code,
       (SELECT COUNT(*) FROM fact_market_timeseries f WHERE f.interval_id = i.interval_id) AS candles,
       (SELECT COUNT(*) FROM fact_market_indicator  f WHERE f.interval_id = i.interval_id) AS indicators
  FROM dim_interval i ORDER BY i.interval_code;

COMMIT;
