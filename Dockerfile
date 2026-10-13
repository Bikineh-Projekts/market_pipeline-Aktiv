# =====================================================================
#  Read-only status dashboard for the market pipeline.
#
#  This image serves the web dashboard. It does NOT run the ETL -- that
#  lives in GitHub Actions. Every database transaction the app opens is
#  READ ONLY, so this container cannot write to the database.
#
#  Build:  docker build -t market-pipeline-web .
#  Run:    docker run --rm -p 8000:8000 \
#            -e DATABASE_URL='postgresql://...?sslmode=require' \
#            market-pipeline-web
#
#  On Render this is selected by render.yaml (runtime: docker).
# =====================================================================

FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: no .pyc files in a container that is rebuilt
#   on every deploy anyway.
# PYTHONUNBUFFERED: without it, Python buffers stdout when it is a pipe,
#   and Render's log stream shows nothing until the buffer flushes --
#   which makes a crash on startup look like silence.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, in their own layer: application code changes on
# almost every deploy, requirements rarely do, so this layer stays cached
# and most deploys skip the install entirely.
#
# No build toolchain is installed because none is needed: psycopg2-binary
# ships prebuilt wheels. Using plain `psycopg2` instead would require gcc
# and libpq-dev here.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# Only what the web service actually serves. The ETL package, the SQL
# migrations, the monitor and the tests are deliberately absent: a
# read-only dashboard has no use for them, and leaving them out keeps the
# image small and the container unable to run a collector by accident.
COPY web/ ./web/

# Run unprivileged. Nothing here needs root, and a web service reachable
# from the internet should not have it.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# Documentation only; the real port comes from $PORT at runtime, which
# Render sets and web/gunicorn.conf.py reads.
EXPOSE 8000
ENV PORT=8000

# For `docker run` and `docker compose`. Render uses healthCheckPath from
# render.yaml instead of this, so the two are set to the same path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('PORT','8000')}/health\", timeout=4).status == 200 else 1)"

# Exec form, so gunicorn is PID 1 and receives Render's SIGTERM directly.
# With the shell form it would be a child of /bin/sh, the signal would go
# to the shell, and the container would be killed after the grace period
# instead of shutting down cleanly.
CMD ["gunicorn", "web.app:create_app()", "--config", "web/gunicorn.conf.py"]
