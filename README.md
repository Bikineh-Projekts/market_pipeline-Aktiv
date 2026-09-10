<p align="right"><a href="README.de.md">🇩🇪 Deutsch</a> · <b>🇬🇧 English</b></p>

# 📈 Market Terminal Pro

A live market dashboard with its own data pipeline: Python collectors pull prices, fundamentals, and technical indicators from multiple APIs, store them in PostgreSQL, and a lightweight vanilla-JS frontend displays them in real time — including pipeline monitoring and a built-in database explorer.

**Live demo:** [market-pipeline.onrender.com](https://market-pipeline.onrender.com/)

---

## ✨ Features

The dashboard is split into four screens (bottom navigation):

| Screen | Description |
|---|---|
| **Market** | Comparison chart for AAPL · MSFT · GOOGL with real/normalized mode, time-range filter (1H–24H), crosshair with per-symbol price tooltip |
| **Stats** | KPI dashboard: pipeline performance chart, reliability goal, coverage insight, API-call/success-rate/latency KPIs, activity bar chart |
| **Pipeline** | Success-rate ring, latency log, system status per data source (PostgreSQL, Finnhub, Alpha Vantage, Twelve Data), live activity feed of collector events |
| **Tables** | Interactive DB explorer: browse, sort, filter, and paginate all tables (DIM/FACT/LOG) |

The frontend is a single `index.html` (vanilla JS + [Chart.js](https://www.chartjs.org/)), no build step, mobile-first with safe-area support.

---

## 🏗️ Architecture

```
                 ┌─────────────────┐
                 │   index.html     │  ← Dashboard (vanilla JS, Chart.js)
                 │  (Market Terminal│
                 │       Pro)       │
                 └────────▲─────────┘
                          │ fetch /api/tables
                          │       /api/table/<name>
                 ┌────────┴─────────┐
                 │   Backend / API   │  ← serves table metadata & rows
                 └────────▲─────────┘
                          │
                 ┌────────┴─────────┐
                 │   PostgreSQL      │  market_db
                 │  dim_ / fact_ /   │
                 │     log_ tables   │
                 └────────▲─────────┘
                          │ INSERT ... ON CONFLICT DO UPDATE
        ┌─────────────────┼─────────────────┐
        │                 │                 │
┌───────┴──────┐  ┌───────┴───────┐  ┌──────┴────────┐
│  finnhub.py  │  │alphavantage.py│  │ twelvedata.py │
│ Quote,       │  │ RSI, MACD,    │  │ OHLCV         │
│ Fundamentals,│  │ EMA, SMA      │  │ (1day, 1min)  │
│ Earnings     │  │               │  │               │
└──────────────┘  └───────────────┘  └───────────────┘
```

### Collectors (`collectors/`)

| File | Source | Writes to |
|---|---|---|
| `finnhub.py` | [Finnhub](https://finnhub.io/) | `fact_market_quote`, `fact_company_fundamental`, `fact_earnings_calendar`, `log_api_call` |
| `alphavantage.py` | [Alpha Vantage](https://www.alphavantage.co/) | `fact_market_indicator` (RSI, MACD, EMA, SMA) |
| `twelvedata.py` | [Twelve Data](https://twelvedata.com/) | `fact_market_timeseries` (OHLCV, 1day & 1min) |

All collectors use `psycopg2` with `ON CONFLICT DO UPDATE` (upsert), so repeated runs never produce duplicates. Symbol, source, interval, and indicator IDs are resolved (or created on demand) via helper functions in `db/connection.py` (`get_symbol_id`, `get_source_id`, `get_interval_id`, `get_indicator_id`).

---

## 🚀 Setup

### Requirements
- Python 3.10+
- PostgreSQL database (`market_db`)
- API keys for Finnhub, Alpha Vantage, and Twelve Data

### Environment variables

```bash
# API keys
FINNHUB_API_KEY=...
ALPHAVANTAGE_API_KEY=...
TWELVEDATA_API_KEY=...

# Database
DATABASE_URL=postgresql://user:password@host:5432/market_db
```

### Installation

```bash
git clone https://github.com/Miladnd01/market-pipeline.git
cd market-pipeline
pip install -r requirements.txt
```

### Running the collectors manually

```python
from collectors import finnhub, alphavantage, twelvedata

for symbol in ["AAPL", "MSFT", "GOOGL"]:
    finnhub.run(symbol)
    alphavantage.run(symbol, interval="daily")
    twelvedata.run(symbol)
```

> ⏱️ Alpha Vantage (free tier: 25 requests/day) deliberately waits 15s between indicator calls; Twelve Data waits 8s between intervals — tuned to each provider's rate limits.

### Opening the dashboard locally

`index.html` is static and loads data via `/api/tables` and `/api/table/<name>` — this requires a backend/API server running on the same origin (see the `API` constant in the `<script>` block). Without a backend, the **Market** screen still shows synthetic sample prices, and the **Tables** screen reports `OFFLINE`.

---

## 🗄️ Database schema (convention)

| Prefix | Meaning | Examples |
|---|---|---|
| `dim_` | Reference/dimension tables | symbols, sources, intervals, indicators |
| `fact_` | Fact data (time series, metrics) | `fact_market_quote`, `fact_market_timeseries`, `fact_market_indicator`, `fact_company_fundamental`, `fact_earnings_calendar` |
| `log_` | Log data | `log_api_call` (endpoint, HTTP status, response time, error) |

The **Tables** screen in the dashboard highlights PK/FK columns, supports full-text search (server-side via `ILIKE`), per-column sorting, and dynamic filters (date, symbol ID, indicator ID, interval ID, endpoint, HTTP status).

---

## 🎨 Frontend stack

- **Pure HTML/CSS/JS** — no build step, no frameworks
- **[Chart.js 4](https://www.chartjs.org/)** for the price chart, pipeline performance chart, and success ring
- **Inline SVG icons** (no external icon font, works offline/CSP-safe)
- **IBM Plex Mono / IBM Plex Sans** as typefaces
- Dark theme, mobile-first, `env(safe-area-inset-*)` for notch devices

---

## 📜 License & credits

© Milan Bikineh · All rights reserved · 2025
