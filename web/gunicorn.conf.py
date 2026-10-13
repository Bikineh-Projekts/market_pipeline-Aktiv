"""
Gunicorn settings for the Render web service.

Sized for a free instance: two sync workers with two threads each is enough
for a dashboard that one or two people open, and it stays inside both the
instance's memory and the database's connection allowance. WEB_DB_POOL_MAX
(default 4) is per worker, so keep the two numbers in mind together.
"""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = int(os.getenv("WEB_WORKERS", "2"))
threads = int(os.getenv("WEB_THREADS", "2"))
worker_class = "gthread"

# A free instance has to wake from spin-down, and the first request pays for
# the database waking up too, so allow more than the 30-second default.
timeout = 60
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sus'
preload_app = False  # each worker builds its own pool
