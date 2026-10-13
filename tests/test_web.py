"""
The read-only dashboard.

Two properties matter more than any rendering detail:

  1. It cannot write. GitHub Actions is the only writer; a second one
     duplicates rows and spends the API quota twice as fast.
  2. It survives a wake-from-sleep. A free Render instance is spun down
     after 15 minutes, and its pooled sockets are dead when it comes back.
"""

from __future__ import annotations

import importlib
import json

import psycopg2
import pytest

from tests.conftest import TEST_URL


@pytest.fixture
def web(monkeypatch, pipeline):
    """
    The Flask app wired to the test database, with real pipeline data in it.

    Depends on `pipeline` so the schema exists and a run has happened --
    an empty database is covered by its own tests below.
    """
    pipeline.run.main(["--full", "--trigger", "web-test"])

    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    from web import db as webdb
    importlib.reload(webdb)
    from web import app as webapp
    importlib.reload(webapp)

    webdb.close_pool()
    client = webapp.create_app().test_client()
    yield client, webdb
    webdb.close_pool()


def _kill_connections() -> int:
    """Terminate the test database's backends, as a spin-down would."""
    admin = psycopg2.connect(TEST_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(pg_terminate_backend(pid))
                  FROM pg_stat_activity
                 WHERE datname = current_database() AND pid <> pg_backend_pid()
                """
            )
            return cur.fetchone()[0]
    finally:
        admin.close()


# ------------------------------------------------------------------ routes
def test_health_is_spelled_health(web):
    """The old project pinged /healt and got a 404 288 times a day."""
    client, _ = web
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert client.get("/healt").status_code == 404


def test_dashboard_renders_every_section(web):
    client, _ = web
    html = client.get("/").data.decode()
    for section in ("Market pipeline", "Newest quote", "Latest price per symbol",
                    "Recent runs", "Sources, last 24 hours",
                    "History held per symbol"):
        assert section in html, f"missing: {section}"


def test_dashboard_shows_the_real_company_names(web):
    """Guards the dimension bug from the far side: what a person sees."""
    html = client_html = web[0].get("/").data.decode()
    assert "Apple Inc" in client_html
    assert "UNKNOWN" not in html
    assert "Uncategorized" not in html


def test_api_status_is_json_serialisable(web):
    """Decimal and datetime leak out of psycopg2 and break jsonify."""
    client, _ = web
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    json.dumps(payload)  # raises if anything is not serialisable
    assert payload["state"] in {"healthy", "degraded", "stale", "failing",
                                "no data", "unknown"}
    assert payload["quotes"] and payload["runs"]


def test_api_and_page_agree(web):
    client, _ = web
    state = client.get("/api/status").get_json()["state"]
    assert state.capitalize() in client.get("/").data.decode()


# --------------------------------------------------------------- read-only
@pytest.mark.parametrize("statement", [
    "INSERT INTO dim_symbol (symbol_code) VALUES ('HACK')",
    "UPDATE dim_symbol SET company_name = 'x'",
    "DELETE FROM dim_symbol",
    "CREATE TABLE should_not_exist (id int)",
    "DROP VIEW bi_dim_symbol",
    "TRUNCATE log_api_call",
])
def test_the_dashboard_cannot_write(web, statement):
    _, webdb = web
    with pytest.raises(psycopg2.Error) as excinfo:
        with webdb.read_cursor() as cur:
            cur.execute(statement)
    assert isinstance(excinfo.value, psycopg2.errors.ReadOnlySqlTransaction)


def test_rejected_writes_leave_no_trace(web, pipeline):
    _, webdb = web
    for statement in ("INSERT INTO dim_symbol (symbol_code) VALUES ('HACK')",
                      "CREATE TABLE should_not_exist (id int)"):
        with pytest.raises(psycopg2.Error):
            with webdb.read_cursor() as cur:
                cur.execute(statement)

    assert pipeline.scalar(
        "SELECT COUNT(*) FROM dim_symbol WHERE symbol_code = 'HACK'") == 0
    assert pipeline.scalar(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'should_not_exist'") == 0
    assert pipeline.scalar(
        "SELECT COUNT(*) FROM information_schema.views "
        "WHERE table_name = 'bi_dim_symbol'") == 1


def test_reads_still_work_after_a_rejected_write(web):
    client, webdb = web
    with pytest.raises(psycopg2.Error):
        with webdb.read_cursor() as cur:
            cur.execute("DELETE FROM dim_symbol")
    assert client.get("/").status_code == 200


# ------------------------------------------------------- wake from a sleep
def test_first_request_after_a_spin_down_succeeds(web):
    """
    Without discarding dead connections and retrying once, this is the
    behaviour a visitor gets: an error page, then a working page on reload.
    """
    client, _ = web
    assert client.get("/").status_code == 200   # warm the pool

    _kill_connections()
    assert client.get("/").status_code == 200, (
        "a visitor should not see an error page after the instance wakes")


def test_repeated_spin_downs_do_not_poison_the_pool(web):
    client, _ = web
    client.get("/")
    for _ in range(3):
        _kill_connections()
        assert client.get("/").status_code == 200


def test_health_and_api_survive_a_spin_down(web):
    client, _ = web
    client.get("/health")
    _kill_connections()
    assert client.get("/health").status_code == 200
    _kill_connections()
    assert client.get("/api/status").status_code == 200


# ---------------------------------------------------------- misconfigured
def test_missing_database_url_explains_itself(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGHOST", raising=False)
    from web import db as webdb
    importlib.reload(webdb)
    from web import app as webapp
    importlib.reload(webapp)

    response = webapp.create_app().test_client().get("/")
    assert response.status_code == 500
    assert "No database configured" in response.data.decode()


def test_unreachable_database_is_unhealthy_not_a_crash(monkeypatch):
    """Render restarts the instance when /health stops returning 200."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:9/none")
    from web import db as webdb
    importlib.reload(webdb)
    from web import app as webapp
    importlib.reload(webapp)

    client = webapp.create_app().test_client()
    assert client.get("/health").status_code == 503
    page = client.get("/")
    assert page.status_code == 500
    assert "Could not read the database" in page.data.decode()


# ------------------------------------------------------------ url handling
def test_sslmode_is_added_for_a_remote_host(monkeypatch):
    """Render's external URL omits it, and psycopg2's default then fails."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://u:p@dpg-abc-a.frankfurt-postgres.render.com/marketdb")
    from web import db as webdb
    importlib.reload(webdb)
    assert "sslmode=require" in webdb.database_url()


def test_existing_query_parameters_are_preserved(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://u:p@dpg-abc-a.frankfurt-postgres.render.com/db?application_name=x")
    from web import db as webdb
    importlib.reload(webdb)
    url = webdb.database_url()
    assert "application_name=x" in url and "sslmode=require" in url


def test_an_explicit_sslmode_is_not_overridden(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://u:p@dpg-abc-a.frankfurt-postgres.render.com/db?sslmode=verify-full")
    from web import db as webdb
    importlib.reload(webdb)
    url = webdb.database_url()
    assert "sslmode=verify-full" in url
    assert "sslmode=require" not in url


def test_ssl_is_not_forced_on_a_local_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/x")
    from web import db as webdb
    importlib.reload(webdb)
    assert "sslmode" not in webdb.database_url()

