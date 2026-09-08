# Market Data Pipeline — GitHub Actions + Databricks PostgreSQL

این نسخه برای اجرای کامل Pipeline روی GitHub Actions آماده شده است. هیچ VPS دائمی لازم نیست.

## ساختار

```text
.github/workflows/etl.yml
.github/workflows/database.yml
.github/workflows/maintenance.yml
pipeline/run.py
pipeline/config.py
pipeline/db.py
pipeline/collectors/*.py
sql/01_schema.sql
sql/02_bi_views.sql
sql/03_maintenance.sql
monitor/monitor.mjs
monitor/package.json
```

## 1) GitHub Secrets

Repository → **Settings → Secrets and variables → Actions → Secrets**

بسازید:

```text
PGHOST=ep-lively-frost-d8z977xq.database.us-east-2.cloud.databricks.com
PGDATABASE=marketdb
PGUSER=mohammadhossein.bikineh@uni-rostock.de
PGPASSWORD=<رمز واقعی دیتابیس>
FINNHUB_API_KEY=<...>
TWELVEDATA_API_KEY=<...>
ALPHAVANTAGE_API_KEY=<...>
SYMBOLS=AAPL,MSFT,GOOGL
```

**PGPASSWORD را هرگز داخل کد یا Git commit نکنید.** مقدار `${PGPASSWORD}` که در نمونه شما آمده فقط placeholder است؛ در Secret باید خود رمز واقعی قرار بگیرد.

## 2) GitHub Variables

Repository → **Settings → Secrets and variables → Actions → Variables**

پیشنهاد:

```text
PGPORT=5432
PGSSLMODE=require
INTRADAY_ENABLED=true
INTRADAY_INTERVAL=5min
INTRADAY_OUTPUTSIZE=30
DAILY_OUTPUTSIZE=100
INDICATOR_MAX_RECORDS=30
STORAGE_CAP_MB=512
MAX_QUOTE_AGE_MIN=90
MIN_SUCCESS_RATE=80
```

## 3) راه‌اندازی اولیه دیتابیس

بعد از Push:

**Actions → Database → Run workflow → action = migrate**

برای Backfill:

**Actions → Database → Run workflow → action = backfill**

مثلاً:

```text
symbols: AAPL,MSFT,GOOGL
history: 1000
```

## 4) ETL زمان‌بندی‌شده

`etl.yml` از دو نوع اجرا استفاده می‌کند:

- در روزهای دوشنبه تا جمعه، هر ۳۰ دقیقه در پنجره زمانی پوشش‌دهنده بازار آمریکا اجرا می‌شود و کد داخلی زمان نیویورک را بررسی می‌کند.
- هر شب ساعت 22:30 UTC یک Full Run اجرا می‌شود و `--full` را فعال می‌کند؛ بنابراین fundamentals، earnings و Alpha Vantage فقط در همین اجرای روزانه مصرف می‌شوند.

آخر هفته‌ها هیچ Scheduleای وجود ندارد.

## 5) اجرای دستی

**Actions → ETL → Run workflow**

سه ورودی دارد:

- `symbols`: مثلاً `AAPL,MSFT,TSLA`
- `full`: فعال کردن Full Run
- `force`: اجرای دستی حتی خارج از ساعات بازار

## 6) نگهداری

`maintenance.yml` در روز اول هر ماه اجرا می‌شود و:

- API log قدیمی را حذف می‌کند.
- Intraday قدیمی را حذف می‌کند.
- Quote قدیمی را حذف می‌کند.
- Run log قدیمی را حذف می‌کند.
- `VACUUM (ANALYZE)` را اجرا می‌کند.
- حجم دیتابیس را گزارش می‌کند.

## نکته مهم درباره APIهای رایگان

Alpha Vantage در این پروژه عمداً فقط در Full Run اجرا می‌شود تا سهمیه روزانه زود مصرف نشود. Intraday نیز بیشترین رشد حجم را ایجاد می‌کند؛ برای کاهش ذخیره می‌توانید `INTRADAY_ENABLED=false` قرار دهید.
