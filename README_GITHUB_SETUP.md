<p align="right"><b>🇩🇪 Deutsch</b> · <a href="README.md">🇬🇧 English</a></p>

# 📈 Market Terminal Pro

Ein Live-Markt-Dashboard mit eigener Datenpipeline: Python-Collectoren ziehen Kursdaten, Fundamentaldaten und technische Indikatoren von mehreren APIs, speichern sie in PostgreSQL, und ein leichtgewichtiges Vanilla-JS-Frontend zeigt sie in Echtzeit an — inklusive Pipeline-Monitoring und einem eingebauten Datenbank-Explorer.

**Live-Demo:** [market-pipeline.onrender.com](https://market-pipeline.onrender.com/)

---

## ✨ Features

Das Dashboard ist in vier Screens aufgeteilt (untere Navigation):

| Screen | Beschreibung |
|---|---|
| **Market** | Vergleichs-Chart für AAPL · MSFT · GOOGL mit Live-/Normalisiert-Modus, Zeitraum-Filter (1H–24H), Crosshair mit Preis-Tooltip pro Symbol |
| **Stats** | KPI-Dashboard: Pipeline-Performance-Chart, Reliability-Goal, Coverage-Insight, API-Call/Success-Rate/Latenz-KPIs, Activity-Balkendiagramm |
| **Pipeline** | Success-Rate-Ring, Latency-Log, System-Status je Datenquelle (PostgreSQL, Finnhub, Alpha Vantage, Twelve Data), Live-Activity-Feed der Collector-Events |
| **Tables** | Interaktiver DB-Explorer: alle Tabellen (DIM/FACT/LOG) durchsuchen, sortieren, filtern, paginieren |

Das Frontend ist eine einzelne `index.html` (Vanilla JS + [Chart.js](https://www.chartjs.org/)), ohne Build-Step, mobile-first mit Safe-Area-Unterstützung.

---

## 🏗️ Architektur

```
                 ┌─────────────────┐
                 │   index.html     │  ← Dashboard (Vanilla JS, Chart.js)
                 │  (Market Terminal│
                 │       Pro)       │
                 └────────▲─────────┘
                          │ fetch /api/tables
                          │       /api/table/<name>
                 ┌────────┴─────────┐
                 │   Backend / API   │  ← liefert Tabellen-Metadaten & Zeilen
                 └────────▲─────────┘
                          │
                 ┌────────┴─────────┐
                 │   PostgreSQL      │  market_db
                 │  dim_ / fact_ /   │
                 │     log_ Tabellen │
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

### Collectoren (`collectors/`)

| Datei | Quelle | Schreibt in |
|---|---|---|
| `finnhub.py` | [Finnhub](https://finnhub.io/) | `fact_market_quote`, `fact_company_fundamental`, `fact_earnings_calendar`, `log_api_call` |
| `alphavantage.py` | [Alpha Vantage](https://www.alphavantage.co/) | `fact_market_indicator` (RSI, MACD, EMA, SMA) |
| `twelvedata.py` | [Twelve Data](https://twelvedata.com/) | `fact_market_timeseries` (OHLCV, 1day & 1min) |

Alle Collectoren nutzen `psycopg2` mit `ON CONFLICT DO UPDATE` (Upsert), sodass wiederholte Läufe keine Duplikate erzeugen. Symbol-, Source-, Interval- und Indicator-IDs werden über Hilfsfunktionen in `db/connection.py` (`get_symbol_id`, `get_source_id`, `get_interval_id`, `get_indicator_id`) aufgelöst bzw. bei Bedarf angelegt.

---

## 🚀 Setup

### Voraussetzungen
- Python 3.10+
- PostgreSQL-Datenbank (`market_db`)
- API-Keys für Finnhub, Alpha Vantage und Twelve Data

### Environment-Variablen

```bash
# API Keys
FINNHUB_API_KEY=...
ALPHAVANTAGE_API_KEY=...
TWELVEDATA_API_KEY=...

# Datenbank
DATABASE_URL=postgresql://user:password@host:5432/market_db
```

### Installation

```bash
git clone https://github.com/Miladnd01/market-pipeline.git
cd market-pipeline
pip install -r requirements.txt
```

### Collectoren manuell ausführen

```python
from collectors import finnhub, alphavantage, twelvedata

for symbol in ["AAPL", "MSFT", "GOOGL"]:
    finnhub.run(symbol)
    alphavantage.run(symbol, interval="daily")
    twelvedata.run(symbol)
```

> ⏱️ Alpha Vantage (kostenloses Tier: 25 Requests/Tag) wartet bewusst 15s zwischen Indikator-Abrufen; Twelve Data 8s zwischen Intervallen — auf die jeweiligen Rate-Limits abgestimmt.

### Dashboard lokal öffnen

`index.html` ist statisch und lädt Daten über `/api/tables` bzw. `/api/table/<name>` — dafür muss ein Backend/API-Server unter derselben Origin laufen (siehe `API`-Konstante im `<script>`-Block). Ohne Backend zeigt der **Market**-Screen weiterhin synthetische Beispielkurse an, der **Tables**-Screen meldet `OFFLINE`.

---

## 🗄️ Datenbankschema (Konvention)

| Präfix | Bedeutung | Beispiele |
|---|---|---|
| `dim_` | Stammdaten / Dimensionstabellen | Symbole, Quellen, Intervalle, Indikatoren |
| `fact_` | Faktendaten (Zeitreihen, Kennzahlen) | `fact_market_quote`, `fact_market_timeseries`, `fact_market_indicator`, `fact_company_fundamental`, `fact_earnings_calendar` |
| `log_` | Protokolldaten | `log_api_call` (Endpoint, HTTP-Status, Response-Zeit, Fehler) |

Der **Tables**-Screen im Dashboard zeigt PK/FK-Spalten farblich hervorgehoben, unterstützt Volltextsuche (server-seitig via `ILIKE`), spaltenweise Sortierung und dynamische Filter (Datum, Symbol-ID, Indicator-ID, Interval-ID, Endpoint, HTTP-Status).

---

## 🎨 Frontend-Stack

- **Reines HTML/CSS/JS** — kein Build-Step, keine Frameworks
- **[Chart.js 4](https://www.chartjs.org/)** für Preis-Chart, Pipeline-Performance-Chart und Success-Ring
- **Inline-SVG-Icons** (kein externer Icon-Font, funktioniert offline/CSP-sicher)
- **IBM Plex Mono / IBM Plex Sans** als Schriftarten
- Dark-Theme, mobile-first, `env(safe-area-inset-*)` für Notch-Geräte

---

## 📜 Lizenz & Credits

© Milan Bikineh · Alle Rechte vorbehalten · 2025
