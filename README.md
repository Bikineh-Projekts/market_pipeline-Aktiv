# Market Pipeline

خط لوله‌ی داده‌ی بازار سهام روی زیرساخت کاملاً رایگان: GitHub Actions به‌عنوان موتور، Neon Postgres به‌عنوان انبار، Power BI برای تحلیل، و یک مانیتور جاوااسکریپتی برای اینکه بفهمید کار می‌کند یا نه.

---

## جواب مستقیم سؤال شما درباره‌ی Neon

سه چیز پرسیدید. جواب هر کدام جداگانه:

### «باز همون محدودیت به وجود میاد؟»

نه، چون محدودیت Neon از جنس دیگری است. Render رایگان یک **سرور** به شما می‌داد که می‌خوابید و باید بیدارش می‌کردید. Neon اصلاً سرور نمی‌فروشد؛ ذخیره‌سازی و پردازش را جدا کرده. داده همیشه سرجایش است، فقط پردازنده وقتی کاری نیست خاموش می‌شود.

محدودیت‌های واقعی نسخه‌ی رایگان: ۱۰۰ ساعت-CU پردازش در ماه به‌ازای هر پروژه، نیم گیگ فضا، و ۵ گیگ ترافیک خروجی. اگر از سقف پردازش رد شوید، پردازش تا شروع دوره‌ی بعد معلق می‌شود — ولی داده پاک نمی‌شود. اگر از سقف فضا رد شوید، فقط نوشتن جدید رد می‌شود تا وقتی جا باز کنید.

### «پروژم نمی‌خوابه؟»

پردازش **می‌خوابد** — بعد از ۵ دقیقه بی‌کاری، و در نسخه‌ی رایگان نمی‌شود خاموشش کرد. ولی این با خوابیدن Render فرق بنیادی دارد:

| | Render رایگان | Neon رایگان |
|---|---|---|
| چه چیزی می‌خوابد | خود سرویس وب | فقط پردازنده‌ی دیتابیس |
| بیدار شدن | ۳۰ تا ۵۰ ثانیه | چند صد میلی‌ثانیه، خودکار |
| نیاز به ping نگه‌دارنده | دارد | **ندارد** |
| داده هنگام خواب | باقی می‌ماند ولی سرویس در دسترس نیست | همیشه در دسترس |

یعنی جوابش این است: بله می‌خوابد، و اتفاقاً همین چیزی است که شما می‌خواهید. هر بار که ETL وصل می‌شود، دیتابیس در کسری از ثانیه بیدار می‌شود، کارش را می‌کند، و دوباره می‌خوابد. هیچ workflow نگه‌دارنده‌ای لازم نیست — همان `ping-render.yml` که ۲۸۸ بار در روز اجرا می‌شد (و ضمناً به `/healt` می‌زد که آدرس اشتباهی بود) کلاً حذف می‌شود.

مصرف واقعی شما با زمان‌بندی این پروژه:

```
۱۷ اجرا در روز کاری × ۲۲ روز   = ۳۷۴ اجرا در ماه
هر اجرا: ~۱ دقیقه فعال + ۵ دقیقه تا خوابیدن = ۶ دقیقه
۳۷۴ × ۶ دقیقه                  = ۳۷ ساعت در ماه
با کوچک‌ترین اندازه‌ی پردازش   ≈ ۹ ساعت-CU از ۱۰۰
```

یعنی حدود **۹ درصد** سهمیه. جای زیادی دارید. جایی که این عدد می‌ترکد، اجرای هر ۵ دقیقه است: آن‌وقت به ۳۳ ساعت در روز می‌رسد که از سقف ماهانه رد می‌شود. فاصله‌ی زمان‌بندی مهم است، نه خود Neon.

### «داده هر چند وقت یک‌بار به‌روز می‌شود؟»

دو زمان‌بندی جدا دارد، عمداً متفاوت:

| زمان‌بندی | چه می‌آورد | چرا |
|---|---|---|
| هر ۳۰ دقیقه، ۱۴ تا ۲۱ UTC، دوشنبه تا جمعه | قیمت لحظه‌ای + کندل روزانه و ۵ دقیقه‌ای | سبک و ارزان، ساعات بازار آمریکا |
| هر شب، ۲۲:۳۰ UTC | همه‌ی بالا + داده‌ی بنیادی + تقویم سود + اندیکاتورها | Alpha Vantage روزی فقط ۲۵ درخواست می‌دهد |

آن جدول دوم دلیل فنی دارد. هر نماد چهار اندیکاتور یعنی چهار درخواست؛ با ۳ نماد می‌شود ۱۲ تا از ۲۵ تا. اگر اندیکاتورها را روی زمان‌بندی نیم‌ساعته می‌گذاشتیم، سهمیه تا ساعت ۹ صبح تمام می‌شد. برای همین به اجرای شبانه منتقل شده‌اند.

می‌خواهید سریع‌تر باشد؟ در `.github/workflows/etl.yml` مقدار `cron` را عوض کنید. کمترین فاصله‌ی مجاز گیت‌هاب ۵ دقیقه است، ولی توصیه نمی‌کنم: هم سهمیه‌ی Neon را می‌خورد، هم سهمیه‌ی دقیقه‌های Actions در مخزن خصوصی را.

---

## معماری

```
GitHub Actions              Neon Postgres            Power BI
   «موتور»                    «انبار»                 «نما»

  etl.yml       ──┐
  (زمان‌بندی)      │      ┌──────────────┐        ┌────────────┐
                  ├─────▶│  dim_ / fact_ │───────▶│ ویوهای bi_ │
  pipeline/run.py │      │    log_       │        │  مدل ستاره │
  (پایتون)      ──┘      └──────────────┘        └────────────┘
                                 ▲
  monitor/monitor.mjs ───────────┘
  (سلامت، کد خروج ۰ یا ۱)
```

هیچ سرور دائمی وجود ندارد. هر اجرا یک ماشین تازه است که کارش را می‌کند و می‌میرد.

```
market-pipeline/
├── sql/
│   ├── 01_schema.sql        جدول‌ها، ایندکس‌ها، داده‌ی اولیه
│   ├── 02_bi_views.sql      ۱۶ ویوی آماده‌ی Power BI
│   └── 03_maintenance.sql   پاک‌سازی و VACUUM ماهانه
├── pipeline/
│   ├── config.py            همه‌ی تنظیمات از متغیر محیطی
│   ├── db.py                اتصال، دیمنشن‌ها، لاگ
│   ├── run.py               نقطه‌ی ورود
│   └── collectors/
│       ├── base.py          لایه‌ی HTTP مشترک — هر درخواست لاگ می‌شود
│       ├── finnhub.py       قیمت، بنیادی، تقویم سود
│       ├── twelvedata.py    کندل OHLCV
│       └── alphavantage.py  RSI، MACD، EMA، SMA
├── monitor/monitor.mjs      مانیتور سلامت
├── docs/powerbi_measures.dax
└── .github/workflows/
    ├── etl.yml              زمان‌بندی‌شده
    ├── database.yml         migrate / backfill / verify / reset
    └── maintenance.yml      ماهانه
```

---

## راه‌اندازی

### ۱. دیتابیس

در [neon.com](https://neon.com) با اکانت گیت‌هاب وارد شوید. کارت بانکی نمی‌خواهد. پروژه بسازید و از دکمه‌ی **Connect** رشته‌ی اتصال را بردارید:

```
postgresql://neondb_owner:npg_XXXX@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require
```

`?sslmode=require` را حذف نکنید.

### ۲. Secretها

مخزن → **Settings → Secrets and variables → Actions**:

| نام | مقدار |
|---|---|
| `DATABASE_URL` | همان رشته‌ی کامل |
| `FINNHUB_API_KEY` | [finnhub.io](https://finnhub.io) |
| `ALPHAVANTAGE_API_KEY` | [alphavantage.co](https://www.alphavantage.co/support/#api-key) |
| `TWELVEDATA_API_KEY` | [twelvedata.com](https://twelvedata.com) |
| `SYMBOLS` | اختیاری، پیش‌فرض `AAPL,MSFT,GOOGL` |
| `ALERT_WEBHOOK_URL` | اختیاری، برای هشدار اسلک یا دیسکورد |

برخلاف نسخه‌ی قبلی پروژه، فقط یک متغیر دیتابیس لازم است. `pipeline/db.py` اول `DATABASE_URL` را نگاه می‌کند و اگر نبود سراغ `PGHOST` و بقیه می‌رود.

### ۳. ساخت جدول‌ها

**Actions → Database → Run workflow → action = `migrate`**

هیچ‌چیزی روی کامپیوتر خودتان لازم نیست. این کار جدول‌ها، ایندکس‌ها و ۱۶ ویوی `bi_*` را می‌سازد. همه‌شان `IF NOT EXISTS` یا `CREATE OR REPLACE` هستند، پس هر وقت خواستید بی‌خطر دوباره اجرا کنید.

### ۴. ریختن داده‌ی تاریخی

**Actions → Database → Run workflow → action = `backfill`**، با `history = 1000` (حدود چهار سال کندل روزانه برای هر نماد).

در حالت backfill داده‌ی درون‌روزی عمداً خاموش است — وگرنه ده‌ها هزار ردیف کم‌ارزش وارد می‌شود و بودجه‌ی فضا را می‌خورد.

انتهای لاگ بخش verify را ببینید. اگر `fact_market_timeseries` صفر ماند، بخش «عیب‌یابی» پایین را بخوانید.

⚠️ روزی بیش از یک بار backfill نزنید. سهمیه‌ی ۲۵ درخواستی Alpha Vantage با ۳ نماد نصف می‌شود.

### ۵. روشن کردن زمان‌بندی

`etl.yml` را کامیت کنید. یک بار دستی تستش کنید (**Actions → ETL → Run workflow**) و اگر سبز شد کار تمام است.

---

## Power BI

**Get Data → PostgreSQL** با حالت **Import**. فقط ویوهای `bi_*` را تیک بزنید، نه جدول‌های خام.

روابط، همه یک‌به‌چند و تک‌جهته:

```
bi_dim_date[date_key]        ──< bi_fact_price_daily[date_key]
                             ──< bi_fact_indicator_daily[date_key]
                             ──< bi_fact_api_call[date_key]
                             ──< bi_fact_earnings[date_key]
                             ──< bi_fact_pipeline_run[date_key]

bi_dim_symbol[symbol_id]     ──< bi_fact_price_daily[symbol_id]
                             ──< bi_fact_indicator_daily[symbol_id]
                             ──< bi_fact_quote_latest[symbol_id]
                             ──< bi_fact_fundamental_latest[symbol_id]
                             ──< bi_fact_earnings[symbol_id]

bi_dim_source[source_id]     ──< bi_fact_api_call[source_id]
bi_dim_interval[interval_id] ──< bi_fact_price_daily[interval_id]
```

بعد `bi_dim_date` را انتخاب کنید و **Mark as Date Table** با ستون `full_date`. بدون این، توابع Time Intelligence جواب غلط می‌دهند.

مِژرهای آماده در `docs/powerbi_measures.dax` هستند.

### چرا این ویوها با نسخه‌ی قبلی فرق دارند

سه تصمیم که مستقیم روی سرعت Power BI اثر دارند:

- **هیچ `ORDER BY` داخل ویو نیست.** Power BI مرتب‌سازی ویو را نادیده می‌گیرد ولی هزینه‌اش را می‌پردازد و Query Folding می‌شکند.
- **هیچ ستون `jsonb` بیرون داده نمی‌شود.** Power BI آن را به‌صورت Record غیرقابل‌استفاده وارد می‌کند.
- **اندیکاتورها پیوت شده‌اند.** در `bi_fact_indicator_daily` هر کدام از RSI و MACD و EMA و SMA یک **ستون** است، نه یک ردیف. یعنی می‌توانید RSI و قیمت را بدون DAX پیچیده در یک ویژوال بگذارید.

ستون‌های دسته‌بندی هم آماده‌اند و مستقیم در Slicer می‌روند: `market_cap_band`، `valuation_band`، `volatility_band`، `move_category`، `rsi_signal`، `macd_signal_category`، `trend_category`، `status_category`، `latency_band`، `profitability_band`.

---

## مانیتور

```bash
cd monitor && npm install

node monitor.mjs              # یک بررسی، خروجی رنگی
node monitor.mjs --watch=30   # هر ۳۰ ثانیه
node monitor.mjs --json       # خروجی ماشین‌خوان
node monitor.mjs --notify     # هشدار به ALERT_WEBHOOK_URL
```

```
  MARKET PIPELINE — HEALTH CHECK
  ──────────────────────────────────────────────
  overall status: ▲ WARN
  ──────────────────────────────────────────────

  ✔  Storage                  9.1 MB of 512 MB (1.8%)
  ✔  Data freshness           last quote 12m ago (limit 90m)
  ✔  Last ETL run             success · 3/3 symbols · 74.2s
  ▲  API success rate (24h)   91.4% of 340 calls (29 errors)
  ✔  Average latency (24h)    412 ms
  ✔  ETL runs (24h)           17 runs, 0 failed
  ▲  Symbol coverage          stale: GOOGL
```

مهم‌تر از خروجی رنگی، **کد خروج** است: ۰ برای سالم یا هشدار، ۱ برای خراب. برای همین مستقیم به‌عنوان یک step در `etl.yml` صدا زده می‌شود؛ اگر خط لوله خراب باشد job قرمز می‌شود، گیت‌هاب یک issue باز می‌کند و ایمیل می‌آید. لازم نیست کسی صفحه‌ای را باز کند و نگاه کند.

---

## فضا — عددهای اندازه‌گیری‌شده

هر ردیف `fact_market_timeseries` روی همین اسکیما ۳۸۹ بایت است (اندازه‌گیری روی ۲۰ هزار ردیف واقعی).

با ۳ نماد و تنظیمات پیش‌فرض:

| منبع | حجم ماهانه |
|---|---|
| کندل ۵ دقیقه‌ای | ~۱٫۸ مگابایت |
| لاگ API | ~۱٫۳ مگابایت |
| داده‌ی بنیادی | ~۱٫۴ مگابایت |
| قیمت لحظه‌ای | ~۰٫۶ مگابایت |
| کندل روزانه و اندیکاتور | ناچیز |
| **جمع** | **~۵ مگابایت** |

پیش‌فرض درون‌روزی عمداً `5min` است نه `1min`. با `1min` همین رقم به ۹ مگابایت در ماه می‌رسید — پنج برابر، برای دقتی که در Power BI معمولاً استفاده نمی‌شود.

با ۵ مگابایت در ماه، سقف ۵۱۲ مگابایتی Neon حدود **هشت سال** دوام می‌آورد، و `maintenance.yml` که ماهانه اجرا می‌شود آن را عملاً بی‌نهایت می‌کند.

اگر اصلاً داده‌ی درون‌روزی نمی‌خواهید: در Actions یک Variable به اسم `INTRADAY_ENABLED` با مقدار `false` بسازید. مصرف به زیر ۲ مگابایت در ماه می‌رسد.

**یک نکته درباره‌ی ترافیک:** سهمیه‌ی خروجی رایگان ۵ گیگ در ماه است. رفرش Import در Power BI هر بار کل جدول‌ها را می‌کشد. تا وقتی دیتابیس کوچک است مهم نیست، ولی اگر روزی به ۱۵۰ مگابایت رسید و روزانه رفرش کنید، به سقف نزدیک می‌شوید. رفرش هفتگی یا Incremental Refresh حلش می‌کند.

---

## سه باگی که در نسخه‌ی قبلی بود و اینجا درست شده

**۱. لاگ API فقط موفقیت‌ها را ثبت می‌کرد.** در `collectors/finnhub.py` قدیمی، `_get()` قبل از رسیدن به `_log()` استثنا پرتاب می‌کرد. نتیجه این بود که `log_api_call` هیچ‌وقت چیزی جز کد ۲۰۰ نداشت و نرخ موفقیت همیشه ۱۰۰٪ نشان می‌داد — یعنی دقیقاً وقتی که باید هشدار می‌داد، ساکت بود. حالا هر درخواست از `collectors/base.py` رد می‌شود و در بلوک `finally` لاگ می‌شود، چه موفق چه ناموفق.

**۲. `twelvedata.run()` خطاهایش را می‌بلعید.** حلقه‌اش خطا را چاپ می‌کرد ولی دوباره پرتاب نمی‌کرد، پس در `main.py` این مرحله همیشه `success=True` ثبت می‌شد حتی وقتی هیچ کندلی نیامده بود.

**۳. `fact_company_fundamental` قید یکتا نداشت.** هر اجرا یک ردیف کامل جدید با دو ستون `jsonb` بزرگ اضافه می‌کرد. با اجرای هر نیم‌ساعت یعنی ~۳۵۰ ردیف در ماه به‌ازای هر نماد، از داده‌ای که ماهی یک بار عوض می‌شود. حالا قید `UNIQUE (symbol_id, source_id, fetched_date)` روزی یک ردیف نگه می‌دارد.

ضمناً وضعیت اجرا حالا سه حالت دارد نه دو حالت: `success` وقتی همه‌چیز آمد، `partial` وقتی داده آمد ولی یکی از منابع اصلی خراب بود، و `failed` وقتی هیچ داده‌ای نیامد. تمام‌شدن سهمیه‌ی Alpha Vantage عمداً اجرا را خراب اعلام نمی‌کند ولی در `error_msg` ثبت می‌شود.

---

## عیب‌یابی

| نشانه | علت |
|---|---|
| `DATABASE_URL secret is missing` | secret ساخته نشده یا اسمش غلط است |
| `SSL connection is required` | `?sslmode=require` از انتهای رشته افتاده |
| `password authentication failed` | رمز Neon را ری‌ست و secret را به‌روز کنید |
| workflow سبز است ولی جدول‌ها خالی | جدول `log_api_call` را ببینید؛ ستون `error_msg` دلیل را می‌گوید |
| `relation "bi_..." does not exist` | `migrate` اجرا نشده |
| اندیکاتورها نمی‌آیند | سهمیه‌ی روزانه‌ی Alpha Vantage؛ تا فردا صبر کنید |
| زمان‌بندی خودبه‌خود خاموش شد | در مخزن عمومی، ۶۰ روز بی‌کامیتی. ماهی یک کامیت کافی است |
| `compute suspended` از Neon | سقف ماهانه‌ی ساعت-CU؛ فاصله‌ی cron را زیاد کنید |

برای دیدن اینکه دقیقاً چه اتفاقی افتاده:

```sql
SELECT started_at_utc, status, symbols_ok, symbols_failed, error_msg
  FROM log_pipeline_run ORDER BY run_id DESC LIMIT 10;

SELECT called_at_utc, endpoint, http_status, error_msg
  FROM log_api_call
 WHERE http_status IS NULL OR http_status >= 400
 ORDER BY called_at_utc DESC LIMIT 20;
```

---

## اجرای محلی

```bash
cp .env.example .env      # و پرش کنید
pip install -r requirements.txt

psql "$DATABASE_URL" -f sql/01_schema.sql
psql "$DATABASE_URL" -f sql/02_bi_views.sql

python -m pipeline.run                      # قیمت و کندل
python -m pipeline.run --full               # همه‌چیز
python -m pipeline.run --backfill 2000      # تاریخچه‌ی عمیق
python -m pipeline.run --symbols TSLA,NVDA  # نمادهای دیگر
```
