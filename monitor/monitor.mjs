#!/usr/bin/env node
/**
 * monitor.mjs -- Pipeline health monitor (JavaScript, no HTML).
 *
 * Answers one question: is the ETL working, or is it broken, and why?
 *
 *   node monitor.mjs                 one check, pretty terminal output
 *   node monitor.mjs --json          machine-readable JSON only
 *   node monitor.mjs --watch         re-check every 60s (Ctrl+C to stop)
 *   node monitor.mjs --watch=30      re-check every 30s
 *   node monitor.mjs --notify        also POST the result to ALERT_WEBHOOK_URL
 *
 * Exit code: 0 = OK or WARN, 1 = FAIL. That makes it usable directly as a
 * GitHub Actions step -- the job goes red when the pipeline is unhealthy.
 *
 * Env:
 *   DATABASE_URL          optional Postgres connection string
 *   OR PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD/PGSSLMODE
 *   MAX_QUOTE_AGE_MIN     freshness threshold, default 90
 *   STORAGE_CAP_MB        hosting storage cap, default 512 (Neon free)
 *   MIN_SUCCESS_RATE      API success-rate threshold in %, default 80
 *   ALERT_WEBHOOK_URL     optional Slack/Discord/Telegram-compatible webhook
 */

import pg from "pg";

const { Client } = pg;

// ---------------------------------------------------------------- config
const args = process.argv.slice(2);
const hasFlag = (name) => args.some((a) => a === `--${name}` || a.startsWith(`--${name}=`));
const flagValue = (name, fallback) => {
  const hit = args.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.split("=")[1] : fallback;
};

const AS_JSON = hasFlag("json");
const NOTIFY = hasFlag("notify");
const WATCH = hasFlag("watch");
const WATCH_SECONDS = Number(flagValue("watch", 60));

const CONFIG = {
  databaseUrl: process.env.DATABASE_URL || null,
  pgHost: process.env.PGHOST || null,
  pgPort: Number(process.env.PGPORT ?? 5432),
  pgDatabase: process.env.PGDATABASE || null,
  pgUser: process.env.PGUSER || null,
  pgPassword: process.env.PGPASSWORD || null,
  pgSslMode: process.env.PGSSLMODE || "require",
  maxQuoteAgeMin: Number(process.env.MAX_QUOTE_AGE_MIN ?? 90),
  minSuccessRate: Number(process.env.MIN_SUCCESS_RATE ?? 80),
  webhook: process.env.ALERT_WEBHOOK_URL || null,
  // Neon free plan caps a project at 0.5 GB.
  storageCapMb: Number(process.env.STORAGE_CAP_MB ?? 512),
};

// ---------------------------------------------------------------- colors
const supportsColor = process.stdout.isTTY && !process.env.NO_COLOR;
const paint = (code, s) => (supportsColor ? `\x1b[${code}m${s}\x1b[0m` : s);
const c = {
  green: (s) => paint("32", s),
  red: (s) => paint("31", s),
  yellow: (s) => paint("33", s),
  cyan: (s) => paint("36", s),
  gray: (s) => paint("90", s),
  bold: (s) => paint("1", s),
};

const STATE = {
  OK: { label: "OK", icon: "✔", color: c.green, rank: 0 },
  WARN: { label: "WARN", icon: "▲", color: c.yellow, rank: 1 },
  FAIL: { label: "FAIL", icon: "✖", color: c.red, rank: 2 },
};

// ---------------------------------------------------------------- queries
const SQL = {
  health: `SELECT * FROM bi_pipeline_health`,

  lastRun: `
    SELECT run_uid, trigger_source, status, started_at_utc, finished_at_utc,
           duration_seconds, symbols_total, symbols_ok, symbols_failed, error_msg
      FROM log_pipeline_run
     ORDER BY started_at_utc DESC
     LIMIT 1`,

  recentRuns: `
    SELECT status, COUNT(*)::int AS n
      FROM log_pipeline_run
     WHERE started_at_utc > NOW() - INTERVAL '24 hours'
     GROUP BY status`,

  perSource: `
    SELECT s.source_name,
           COUNT(*)::int AS calls,
           COUNT(*) FILTER (WHERE l.http_status BETWEEN 200 AND 299)::int AS ok,
           ROUND(AVG(l.response_ms)::numeric, 0)::int AS avg_ms,
           MAX(l.called_at_utc) AS last_call
      FROM log_api_call l
      LEFT JOIN dim_source s ON s.source_id = l.source_id
     WHERE l.called_at_utc > NOW() - INTERVAL '24 hours'
     GROUP BY s.source_name
     ORDER BY calls DESC`,

  lastErrors: `
    SELECT l.called_at_utc, s.source_name, l.endpoint, l.http_status, l.error_msg
      FROM log_api_call l
      LEFT JOIN dim_source s ON s.source_id = l.source_id
     WHERE (l.http_status IS NULL OR l.http_status NOT BETWEEN 200 AND 299)
       AND l.called_at_utc > NOW() - INTERVAL '24 hours'
     ORDER BY l.called_at_utc DESC
     LIMIT 5`,

  staleSymbols: `
    SELECT s.symbol_code,
           MAX(q.fetched_at_utc) AS last_seen,
           ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(q.fetched_at_utc))) / 60) AS age_min
      FROM dim_symbol s
      LEFT JOIN fact_market_quote q ON q.symbol_id = s.symbol_id
     GROUP BY s.symbol_code
     ORDER BY last_seen NULLS FIRST`,

  storage: `
    SELECT pg_database_size(current_database()) AS bytes,
           ROUND(pg_database_size(current_database()) / 1048576.0, 1) AS megabytes`,
};

// ---------------------------------------------------------------- helpers
const num = (v) => (v === null || v === undefined ? null : Number(v));
const fmtAge = (min) => {
  if (min === null) return "never";
  if (min < 60) return `${Math.round(min)}m ago`;
  if (min < 1440) return `${(min / 60).toFixed(1)}h ago`;
  return `${(min / 1440).toFixed(1)}d ago`;
};
const pad = (s, n) => String(s ?? "").padEnd(n).slice(0, n);

/**
 * Managed Postgres (Neon, Supabase, Render, Railway) requires SSL.
 * A local instance usually has it compiled out. Honour sslmode= if given.
 */
function useSsl(url) {
  if (process.env.PGSSLMODE === "disable") return false;
  if (process.env.PGSSLMODE && process.env.PGSSLMODE !== "disable") return true;
  if (/[?&]sslmode=disable/i.test(url)) return false;
  if (/[?&]sslmode=/i.test(url)) return true;
  let host = "";
  try {
    host = new URL(url).hostname;
  } catch {
    return true;
  }
  return !["localhost", "127.0.0.1", "::1", ""].includes(host);
}

function worst(checks) {
  return checks.reduce(
    (acc, ch) => (STATE[ch.state].rank > STATE[acc].rank ? ch.state : acc),
    "OK"
  );
}

// ---------------------------------------------------------------- checks
function buildChecks({ health, lastRun, runCounts, sources, staleSymbols, storage }) {
  const checks = [];

  // 0. storage against the hosting cap. On Neon's free plan, exceeding
  // 0.5 GB does not delete anything but every INSERT starts failing,
  // so this needs to be visible well before it happens.
  const mb = num(storage?.megabytes);
  const capMb = CONFIG.storageCapMb;
  const pct = mb === null ? null : (mb / capMb) * 100;
  checks.push({
    name: "Storage",
    state: pct === null ? "WARN" : pct > 90 ? "FAIL" : pct > 70 ? "WARN" : "OK",
    detail:
      pct === null
        ? "could not read database size"
        : `${mb} MB of ${capMb} MB (${pct.toFixed(1)}%)`,
  });

  // 1. data freshness
  const age = num(health?.quote_age_minutes);
  checks.push({
    name: "Data freshness",
    state: age === null ? "FAIL" : age > CONFIG.maxQuoteAgeMin ? "FAIL" : age > CONFIG.maxQuoteAgeMin / 2 ? "WARN" : "OK",
    detail:
      age === null
        ? "no quotes in the database at all"
        : `last quote ${fmtAge(age)} (limit ${CONFIG.maxQuoteAgeMin}m)`,
  });

  // 2. last ETL run
  if (!lastRun) {
    checks.push({ name: "Last ETL run", state: "WARN", detail: "no run recorded yet" });
  } else {
    const state =
      lastRun.status === "success" ? "OK" : lastRun.status === "partial" ? "WARN" : lastRun.status === "running" ? "WARN" : "FAIL";
    checks.push({
      name: "Last ETL run",
      state,
      detail: `${lastRun.status} · ${lastRun.symbols_ok}/${lastRun.symbols_total} symbols · ${
        lastRun.duration_seconds ?? "?"
      }s${lastRun.error_msg ? ` · ${lastRun.error_msg.slice(0, 80)}` : ""}`,
    });
  }

  // 3. API success rate
  const rate = num(health?.success_rate_24h);
  checks.push({
    name: "API success rate (24h)",
    state: rate === null ? "WARN" : rate < CONFIG.minSuccessRate ? "FAIL" : rate < 95 ? "WARN" : "OK",
    detail:
      rate === null
        ? "no API calls logged in the last 24h"
        : `${rate}% of ${health.api_calls_24h} calls (${health.api_errors_24h} errors)`,
  });

  // 4. latency
  const latency = num(health?.avg_latency_ms_24h);
  checks.push({
    name: "Average latency (24h)",
    state: latency === null ? "WARN" : latency > 3000 ? "FAIL" : latency > 1200 ? "WARN" : "OK",
    detail: latency === null ? "no data" : `${latency} ms`,
  });

  // 5. run failure rate
  const failed = runCounts.failed ?? 0;
  const total = Object.values(runCounts).reduce((a, b) => a + b, 0);
  checks.push({
    name: "ETL runs (24h)",
    state: total === 0 ? "WARN" : failed / total > 0.5 ? "FAIL" : failed > 0 ? "WARN" : "OK",
    detail: total === 0 ? "no runs in the last 24h -- is the schedule active?" : `${total} runs, ${failed} failed`,
  });

  // 6. stale symbols
  const stale = staleSymbols.filter(
    (s) => s.age_min === null || Number(s.age_min) > CONFIG.maxQuoteAgeMin
  );
  checks.push({
    name: "Symbol coverage",
    state: stale.length === 0 ? "OK" : stale.length === staleSymbols.length ? "FAIL" : "WARN",
    detail:
      stale.length === 0
        ? `all ${staleSymbols.length} symbols up to date`
        : `stale: ${stale.map((s) => s.symbol_code).join(", ")}`,
  });

  return checks;
}

// ---------------------------------------------------------------- runner
async function collect() {
  const hasPgVars = CONFIG.pgHost && CONFIG.pgDatabase && CONFIG.pgUser && CONFIG.pgPassword;
  if (!CONFIG.databaseUrl && !hasPgVars) {
    throw new Error("Set DATABASE_URL or PGHOST/PGDATABASE/PGUSER/PGPASSWORD");
  }

  const client = CONFIG.databaseUrl
    ? new Client({
        connectionString: CONFIG.databaseUrl,
        ssl: useSsl(CONFIG.databaseUrl) ? { rejectUnauthorized: false } : false,
        statement_timeout: 15000,
      })
    : new Client({
        host: CONFIG.pgHost,
        port: CONFIG.pgPort,
        database: CONFIG.pgDatabase,
        user: CONFIG.pgUser,
        password: CONFIG.pgPassword,
        ssl: CONFIG.pgSslMode === "disable" ? false : { rejectUnauthorized: false },
        statement_timeout: 15000,
      });

  await client.connect();
  try {
    // A single pg Client cannot run queries in parallel -- keep this sequential.
    const q = async (sql, fallback) => {
      try {
        return (await client.query(sql)).rows;
      } catch (e) {
        if (fallback === undefined) throw e;
        return fallback;
      }
    };

    const health = (await q(SQL.health))[0] ?? null;
    const lastRun = (await q(SQL.lastRun, []))[0] ?? null;
    const runRows = await q(SQL.recentRuns, []);
    const sources = await q(SQL.perSource, []);
    const errors = await q(SQL.lastErrors, []);
    const staleSymbols = await q(SQL.staleSymbols, []);
    const storage = (await q(SQL.storage, []))[0] ?? null;

    const runCounts = Object.fromEntries(runRows.map((r) => [r.status, r.n]));
    const checks = buildChecks({ health, lastRun, runCounts, sources, staleSymbols, storage });

    return {
      checked_at: new Date().toISOString(),
      overall: worst(checks),
      checks,
      health,
      last_run: lastRun,
      run_counts_24h: runCounts,
      storage,
      sources,
      recent_errors: errors,
    };
  } finally {
    await client.end();
  }
}

// ---------------------------------------------------------------- output
function render(report) {
  const st = STATE[report.overall];
  const line = "─".repeat(66);

  console.log("");
  console.log(c.bold("  MARKET PIPELINE — HEALTH CHECK"));
  console.log(c.gray(`  ${new Date(report.checked_at).toLocaleString()}`));
  console.log(c.gray(`  ${line}`));
  console.log(`  overall status: ${st.color(c.bold(`${st.icon} ${st.label}`))}`);
  console.log(c.gray(`  ${line}`));
  console.log("");

  for (const ch of report.checks) {
    const s = STATE[ch.state];
    console.log(`  ${s.color(s.icon)}  ${pad(ch.name, 24)} ${c.gray(ch.detail)}`);
  }

  if (report.sources?.length) {
    console.log("");
    console.log(c.bold("  SOURCES (last 24h)"));
    console.log(c.gray(`  ${pad("source", 16)}${pad("calls", 9)}${pad("ok", 9)}${pad("avg ms", 9)}last call`));
    for (const s of report.sources) {
      const okRate = s.calls ? Math.round((s.ok / s.calls) * 100) : 0;
      const colorize = okRate >= 95 ? c.green : okRate >= 80 ? c.yellow : c.red;
      // pad first, colorize after -- ANSI codes would otherwise count as width
      const rateCell = colorize(pad(`${okRate}%`, 9));
      console.log(
        `  ${pad(s.source_name ?? "?", 16)}${pad(s.calls, 9)}${rateCell}${pad(s.avg_ms ?? "-", 9)}` +
          c.gray(s.last_call ? new Date(s.last_call).toLocaleTimeString() : "-")
      );
    }
  }

  if (report.recent_errors?.length) {
    console.log("");
    console.log(c.bold(c.red("  RECENT ERRORS")));
    for (const e of report.recent_errors) {
      console.log(
        `  ${c.gray(new Date(e.called_at_utc).toLocaleTimeString())} ${c.red(
          e.http_status ?? "ERR"
        )} ${pad(e.source_name ?? "?", 14)} ${c.gray((e.error_msg || e.endpoint || "").slice(0, 60))}`
      );
    }
  }

  console.log("");
}

async function notify(report) {
  if (!CONFIG.webhook) return;
  const st = STATE[report.overall];
  const failing = report.checks.filter((ch) => ch.state !== "OK");
  const text =
    `${st.icon} Market Pipeline: ${st.label}\n` +
    (failing.length
      ? failing.map((ch) => `• ${ch.name}: ${ch.detail}`).join("\n")
      : "All checks passed.");

  try {
    const res = await fetch(CONFIG.webhook, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, content: text }), // Slack uses text, Discord uses content
    });
    if (!res.ok) console.error(`[notify] webhook returned ${res.status}`);
  } catch (e) {
    console.error(`[notify] failed: ${e.message}`);
  }
}

async function once() {
  let report;
  try {
    report = await collect();
  } catch (e) {
    report = {
      checked_at: new Date().toISOString(),
      overall: "FAIL",
      checks: [{ name: "Database connection", state: "FAIL", detail: e.message }],
      error: e.message,
    };
  }

  if (AS_JSON) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    render(report);
  }

  if (NOTIFY && report.overall !== "OK") await notify(report);

  return report.overall === "FAIL" ? 1 : 0;
}

async function main() {
  if (!WATCH) {
    process.exit(await once());
  }

  console.log(c.gray(`watching every ${WATCH_SECONDS}s — Ctrl+C to stop`));
  for (;;) {
    if (!AS_JSON) process.stdout.write("\x1b[2J\x1b[H"); // clear screen
    await once();
    await new Promise((r) => setTimeout(r, WATCH_SECONDS * 1000));
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
