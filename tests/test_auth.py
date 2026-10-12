"""
Lakebase credential handling.

The point of these tests is the one-hour wall: a token-based connection
must not die mid-run, and a password role must not be dragged through the
token machinery at all.

No Databricks calls are made -- the two HTTP exchanges are stubbed.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import pytest

from pipeline import auth, config


# ------------------------------------------------------------------ stubs
class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def oauth(monkeypatch):
    """OAuth configured, with the two HTTP calls stubbed and counted."""
    monkeypatch.setattr(config, "DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_ID", "sp-client-id")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_SECRET", "sp-secret")
    monkeypatch.setattr(config, "LAKEBASE_ENDPOINT",
                        "projects/p1/branches/production/endpoints/e1")
    monkeypatch.setattr(config, "TOKEN_REFRESH_MARGIN", 300)

    state = {"workspace_calls": 0, "credential_calls": 0, "lifetime": 3600}

    def fake_post(url, **kwargs):
        if url.endswith("/oidc/v1/token"):
            state["workspace_calls"] += 1
            return FakeResponse(200, {"access_token": "ws-token", "expires_in": 3600})
        if url.endswith("/api/2.0/postgres/credentials"):
            state["credential_calls"] += 1
            expires = time.time() + state["lifetime"]
            return FakeResponse(200, {
                "token": f"dbtoken-{state['credential_calls']}",
                "expire_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
            })
        raise AssertionError(f"unexpected POST to {url}")

    monkeypatch.setattr(auth.requests, "post", fake_post)
    auth.reset_cache()
    yield state
    auth.reset_cache()


# --------------------------------------------------------- when it engages
def test_oauth_is_off_unless_all_four_settings_are_present(monkeypatch):
    """A password role must never be routed through the token machinery."""
    monkeypatch.setattr(config, "DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_ID", "id")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "LAKEBASE_ENDPOINT", "")
    assert config.lakebase_oauth_enabled() is False

    monkeypatch.setattr(config, "LAKEBASE_ENDPOINT", "projects/p/branches/b/endpoints/e")
    assert config.lakebase_oauth_enabled() is True


def test_password_role_setup_leaves_oauth_disabled(monkeypatch):
    for name in ("DATABRICKS_HOST", "DATABRICKS_CLIENT_ID",
                 "DATABRICKS_CLIENT_SECRET", "LAKEBASE_ENDPOINT"):
        monkeypatch.setattr(config, name, "")
    assert config.lakebase_oauth_enabled() is False


# ------------------------------------------------------------- token cache
def test_token_is_minted_once_and_reused(oauth):
    first = auth.database_token()
    second = auth.database_token()

    assert first == second
    assert oauth["credential_calls"] == 1, "a fresh token should be served from cache"


def test_token_is_refreshed_before_it_expires(oauth):
    """
    The whole reason this module exists: a run longer than the token's life
    must get a new credential instead of failing partway through.
    """
    first = auth.database_token()
    assert oauth["credential_calls"] == 1

    # Jump to inside the refresh margin, as a long backfill would.
    auth._token_expires_at = time.time() + config.TOKEN_REFRESH_MARGIN - 1

    second = auth.database_token()
    assert oauth["credential_calls"] == 2
    assert second != first, "a refresh must produce a new credential"


def test_refresh_margin_is_respected(oauth):
    auth.database_token()
    # Comfortably outside the margin: no refresh.
    auth._token_expires_at = time.time() + config.TOKEN_REFRESH_MARGIN + 60
    auth.database_token()
    assert oauth["credential_calls"] == 1


def test_token_is_not_trusted_beyond_an_hour(oauth):
    """A server claiming a very long lifetime should not extend the cache."""
    oauth["lifetime"] = 3600
    auth.database_token()
    assert auth._token_expires_at <= time.time() + 3601


def test_unparseable_expiry_falls_back_to_one_hour(oauth, monkeypatch):
    def odd_post(url, **kwargs):
        if url.endswith("/oidc/v1/token"):
            return FakeResponse(200, {"access_token": "ws"})
        return FakeResponse(200, {"token": "tok", "expire_time": "not-a-timestamp"})

    monkeypatch.setattr(auth.requests, "post", odd_post)
    auth.reset_cache()

    auth.database_token()
    remaining = auth._token_expires_at - time.time()
    assert 3500 < remaining <= 3601


def test_reset_cache_forces_a_new_token(oauth):
    auth.database_token()
    auth.reset_cache()
    auth.database_token()
    assert oauth["credential_calls"] == 2


# ----------------------------------------------------------- error clarity
def test_bad_client_secret_names_the_setting(oauth, monkeypatch):
    monkeypatch.setattr(auth.requests, "post",
                        lambda url, **kw: FakeResponse(401, {"error": "unauthorized"}))
    auth.reset_cache()

    with pytest.raises(auth.AuthError) as excinfo:
        auth.database_token()
    assert "DATABRICKS_CLIENT_ID" in str(excinfo.value)


def test_bad_endpoint_names_the_setting(oauth, monkeypatch):
    def post(url, **kwargs):
        if url.endswith("/oidc/v1/token"):
            return FakeResponse(200, {"access_token": "ws"})
        return FakeResponse(404, {"error": "no such endpoint"})

    monkeypatch.setattr(auth.requests, "post", post)
    auth.reset_cache()

    with pytest.raises(auth.AuthError) as excinfo:
        auth.database_token()
    assert "LAKEBASE_ENDPOINT" in str(excinfo.value)


def test_unreachable_host_is_an_auth_error_not_a_raw_exception(oauth, monkeypatch):
    import requests as real_requests

    def boom(url, **kwargs):
        raise real_requests.ConnectionError("dns failure")

    monkeypatch.setattr(auth.requests, "post", boom)
    auth.reset_cache()

    with pytest.raises(auth.AuthError):
        auth.database_token()


# -------------------------------------------------------- url substitution
BASE_URL = ("postgresql://etl_bot:oldpassword@ep-broad-mountain-d8omojwq"
            ".database.us-east-2.cloud.databricks.com/marketdb?sslmode=require")


def test_token_replaces_only_the_password():
    result = auth.apply_token(BASE_URL, "fresh-token")
    parts = urlsplit(result)

    assert parts.password == "fresh-token"
    assert parts.username == "etl_bot"
    assert parts.hostname == ("ep-broad-mountain-d8omojwq.database."
                              "us-east-2.cloud.databricks.com")
    assert parts.path == "/marketdb"
    assert "sslmode=require" in parts.query
    assert "oldpassword" not in result


def test_sslmode_survives_substitution():
    """Losing sslmode would make Lakebase refuse the connection outright."""
    assert "sslmode=require" in auth.apply_token(BASE_URL, "t")


def test_extra_query_parameters_survive():
    url = BASE_URL + "&channel_binding=require"
    result = auth.apply_token(url, "t")
    assert "channel_binding=require" in result
    assert "sslmode=require" in result


def test_token_with_url_unsafe_characters_round_trips_through_libpq():
    """
    A token containing : / @ would split the URL apart if pasted in raw, so
    it is percent-encoded. Checked with psycopg2's parse_dsn, which is
    libpq's own URI parser -- the thing that will actually read the string --
    rather than asserting on the encoded text.
    """
    from psycopg2.extensions import parse_dsn

    token = "ab/cd:ef@gh"
    parsed = parse_dsn(auth.apply_token(BASE_URL, token))

    assert parsed["password"] == token
    assert parsed["host"].endswith("cloud.databricks.com")
    assert parsed["dbname"] == "marketdb"
    assert parsed["sslmode"] == "require"
    assert parsed["user"] == "etl_bot"


def test_port_is_preserved():
    url = ("postgresql://etl_bot:pw@host.databricks.com:5432/marketdb"
           "?sslmode=require")
    parts = urlsplit(auth.apply_token(url, "t"))
    assert parts.port == 5432


def test_username_falls_back_to_the_client_id(monkeypatch):
    """A service principal connects as its client id."""
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_ID", "sp-client-id")
    url = "postgresql://host.databricks.com/marketdb?sslmode=require"
    assert urlsplit(auth.apply_token(url, "t")).username == "sp-client-id"


def test_explicit_user_overrides():
    parts = urlsplit(auth.apply_token(BASE_URL, "t", user="other_role"))
    assert parts.username == "other_role"


def test_hostless_url_is_rejected():
    with pytest.raises(auth.AuthError):
        auth.apply_token("not-a-url", "t")


# ------------------------------------------------- end to end, real database
def test_connection_is_replaced_when_the_token_nears_expiry(pipeline, monkeypatch):
    """
    The failure this prevents: a backfill runs past the hour, the credential
    dies, and every remaining query fails. get_connection must notice the
    stale credential and reconnect instead of letting queries fail.

    Runs against the real test database. The stub stands in for Databricks
    but the connection it produces is genuine, so the reconnect is real.
    """
    import time as _time

    monkeypatch.setattr(config, "DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_ID", "sp-id")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_SECRET", "sp-secret")
    monkeypatch.setattr(config, "LAKEBASE_ENDPOINT", "projects/p/branches/b/endpoints/e")
    monkeypatch.setattr(config, "TOKEN_REFRESH_MARGIN", 300)

    # The local test database uses trust auth, so the password value is
    # irrelevant -- but it must be non-empty, because an empty token is
    # correctly treated as "no credential" by token_is_fresh().
    stand_in = urlsplit(config.DATABASE_URL).password or "trust-auth-ignores-this"
    mints = {"count": 0}

    def fake_token():
        mints["count"] += 1
        auth._token = stand_in
        auth._token_expires_at = _time.time() + 3600
        return stand_in

    monkeypatch.setattr(auth, "database_token", fake_token)
    pipeline.db.close_connection()

    assert pipeline.scalar("SELECT 1") == 1
    assert mints["count"] == 1, "the first connection should mint a credential"

    first = pipeline.db.get_connection()
    assert mints["count"] == 1, "a fresh credential must not be re-minted"

    # Age the credential into the refresh margin, as a long run would.
    auth._token_expires_at = _time.time() + 100
    assert auth.token_is_fresh() is False

    assert pipeline.scalar("SELECT 1") == 1, "queries must keep working"
    assert mints["count"] == 2, "a stale credential should be refreshed exactly once"
    assert pipeline.db.get_connection() is not first, "connection should be replaced"
    assert first.closed, "the old connection should be closed, not leaked"


def test_no_reconnect_churn_while_the_token_is_fresh(pipeline, monkeypatch):
    """Reconnecting on every query would make every run far slower."""
    import time as _time

    monkeypatch.setattr(config, "DATABRICKS_HOST", "https://example.databricks.com")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_ID", "sp-id")
    monkeypatch.setattr(config, "DATABRICKS_CLIENT_SECRET", "sp-secret")
    monkeypatch.setattr(config, "LAKEBASE_ENDPOINT", "projects/p/branches/b/endpoints/e")

    stand_in = urlsplit(config.DATABASE_URL).password or "trust-auth-ignores-this"
    mints = {"count": 0}

    def fake_token():
        mints["count"] += 1
        auth._token = stand_in
        auth._token_expires_at = _time.time() + 3600
        return stand_in

    monkeypatch.setattr(auth, "database_token", fake_token)
    pipeline.db.close_connection()

    conn = None
    for _ in range(5):
        assert pipeline.scalar("SELECT 1") == 1
        conn = conn or pipeline.db.get_connection()

    assert mints["count"] == 1
    assert pipeline.db.get_connection() is conn, "the connection should be reused"
