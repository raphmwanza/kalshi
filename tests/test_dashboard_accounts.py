"""Encrypted dashboard accounts, session login, and per-cycle loading."""

from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.clients.accounts import KalshiAccount, account_is_live, merge_accounts
from src.journal.account_store import (
    AccountStoreUnavailable,
    MemoryAccountStore,
    fernet_from_env,
    load_cycle_accounts,
    open_account_store,
    private_key_hint,
    reset_store_notice,
)
from src.journal.session import LoginLimiter, seal_session, session_secret


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _csrf(page: str) -> str:
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start:page.index('"', start)]


def _body_slice(pem: str) -> str:
    lines = [line.strip() for line in pem.splitlines() if "PRIVATE" not in line and line.strip()]
    compact = "".join(lines)
    return compact[20:60]


@pytest.fixture
def dashboard_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    for name in (
        "KALSHI_ACCOUNTS",
        "KALSHI_API_KEY",
        "KALSHI_PRIVATE_KEY",
        "KALSHI_PRIVATE_KEY_PATH",
        "KALSHI_API_KEY_ID_1",
        "KALSHI_PRIVATE_KEY_1",
        "KALSHI_API_KEY_1",
        "KEYS_ENCRYPTION_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_fernet_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv("KEYS_ENCRYPTION_KEY", raising=False)
    with pytest.raises(AccountStoreUnavailable, match="not set"):
        fernet_from_env()
    monkeypatch.setenv("KEYS_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(AccountStoreUnavailable, match="not a valid Fernet key"):
        fernet_from_env()


def test_open_store_refuses_sqlite_even_with_a_key(monkeypatch):
    monkeypatch.setenv("KEYS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tennis_journal.db")
    with pytest.raises(AccountStoreUnavailable, match="Postgres"):
        open_account_store()


@pytest.mark.asyncio
async def test_memory_store_encrypts_and_hides_the_private_key(caplog):
    caplog.set_level(logging.DEBUG)
    pem = _pem()
    store = MemoryAccountStore(Fernet(Fernet.generate_key()))
    saved = await store.add_account("main", "key-id-abcdef", pem)
    assert saved.live is False
    assert saved.enabled is True
    assert saved.key_hint == private_key_hint(pem)
    assert saved.key_hint
    assert pem not in repr(saved)
    assert "BEGIN" not in repr(saved)
    listed = await store.list_accounts()
    assert pem not in repr(listed)
    token = store._rows[0]["private_key_encrypted"]
    assert pem not in token
    enabled = await store.enabled_accounts()
    assert enabled[0].private_key.strip() == pem.strip()
    assert enabled[0].live is False
    await store.set_enabled(saved.id, False)
    assert await store.enabled_accounts() == []
    store._rows[0]["private_key_encrypted"] = "not-a-token"
    store._rows[0]["enabled"] = True
    assert await store.enabled_accounts() == []
    assert pem not in caplog.text
    assert "key-id-abcdef" not in caplog.text


def test_account_live_flag_cannot_override_a_dry_worker():
    env = KalshiAccount(name="env")
    stored = KalshiAccount(name="pasted", live=False)
    armed = KalshiAccount(name="armed", live=True)
    assert account_is_live(env, False) is False
    assert account_is_live(stored, False) is False
    assert account_is_live(armed, False) is False
    assert account_is_live(env, True) is True
    assert account_is_live(stored, True) is False
    assert account_is_live(armed, True) is True


def test_merge_keeps_the_environment_account_on_a_name_clash():
    env = KalshiAccount(name="main", api_key="from-env")
    stored = KalshiAccount(name="main", api_key="from-db", live=False)
    other = KalshiAccount(name="other", api_key="from-db", live=False)
    merged = merge_accounts([env], [stored, other])
    assert [account.name for account in merged] == ["main", "other"]
    assert merged[0].api_key == "from-env"


@pytest.mark.asyncio
async def test_cycle_loads_enabled_db_accounts_beside_env(monkeypatch, dashboard_env):
    monkeypatch.setenv("KALSHI_API_KEY", "env-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "line-one")
    pem = _pem()
    store = MemoryAccountStore(Fernet(Fernet.generate_key()))
    kept = await store.add_account("pasted", "pasted-key-id", pem)
    disabled = await store.add_account("paused", "paused-key-id", pem)
    await store.set_enabled(disabled.id, False)
    accounts = await load_cycle_accounts(open_store=lambda: store)
    names = {account.name: account for account in accounts}
    assert "default" in names
    assert names["default"].api_key == "env-key"
    assert names["pasted"].api_key == "pasted-key-id"
    assert names["pasted"].live is False
    assert "paused" not in names
    assert kept.id == 1


@pytest.mark.asyncio
async def test_cycle_uses_only_stored_accounts_when_env_is_unset(monkeypatch, dashboard_env):
    store = MemoryAccountStore(Fernet(Fernet.generate_key()))
    await store.add_account("pasted", "pasted-key-id", _pem())
    accounts = await load_cycle_accounts(open_store=lambda: store)
    assert [account.name for account in accounts] == ["pasted"]


@pytest.mark.asyncio
async def test_cycle_skips_db_when_encryption_key_is_missing(monkeypatch, dashboard_env, caplog):
    caplog.set_level(logging.INFO)
    reset_store_notice()
    monkeypatch.setenv("KALSHI_API_KEY", "env-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "line-one")
    accounts = await load_cycle_accounts()
    assert [account.name for account in accounts] == ["default"]
    assert "refusing to store" in caplog.text
    assert "line-one" not in caplog.text


@pytest.mark.asyncio
async def test_cycle_db_error_does_not_log_the_connection_string(monkeypatch, dashboard_env, caplog):
    caplog.set_level(logging.ERROR)
    monkeypatch.setenv("KALSHI_API_KEY", "env-key")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "line-one")

    class Boom:
        async def ensure_schema(self):
            raise RuntimeError("postgres://user:secret-password@db.internal/kalshi")

        async def close(self):
            return None

    accounts = await load_cycle_accounts(open_store=lambda: Boom())
    assert [account.name for account in accounts] == ["default"]
    assert "secret-password" not in caplog.text
    assert "continuing with environment accounts" in caplog.text


@pytest.mark.asyncio
async def test_runner_uses_per_account_dry_run(monkeypatch):
    from src.strategies.tennis_runner import run_tennis_accounts

    seen = []

    class Capture:
        def __init__(self, client, sofascore, dry_run, account_name, journal):
            seen.append((account_name, dry_run))

        async def run(self, live_matches=None):
            return {"placed": 0, "errors": 0}

    class Sofa:
        last_fetch_blocked = False

        async def get_live_tennis_matches(self):
            return []

    class Dummy:
        async def close(self):
            return None

    monkeypatch.setattr("src.strategies.tennis_runner.TennisSet3Strategy", Capture)
    await run_tennis_accounts(
        [
            KalshiAccount(name="env"),
            KalshiAccount(name="pasted", live=False),
            KalshiAccount(name="armed", live=True),
        ],
        sofascore=Sofa(),
        live=True,
        client_factory=lambda account: Dummy(),
    )
    assert seen == [("env", False), ("pasted", True), ("armed", False)]


def _login(client):
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={"password": "s3cret", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_login_cookie_is_secure_httponly_and_rate_limited(monkeypatch):
    from fastapi.testclient import TestClient

    from src.journal.web import create_app

    async def load_events():
        return []

    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "true")
    secure_app = create_app(load_events, login_limiter=LoginLimiter(max_failures=2, window_seconds=600))
    secure_client = TestClient(secure_app)
    login = secure_client.get("/login")
    flags = {part.strip().lower() for part in login.headers["set-cookie"].split(";")}
    assert "httponly" in flags
    assert "samesite=strict" in flags
    assert "secure" in flags

    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    app = create_app(load_events, login_limiter=LoginLimiter(max_failures=2, window_seconds=600))
    client = TestClient(app)
    login = client.get("/login")
    plain_flags = {part.strip().lower() for part in login.headers["set-cookie"].split(";")}
    assert "secure" not in plain_flags
    token = _csrf(login.text)
    assert client.post("/login", data={"password": "s3cret"}).status_code == 403
    first = client.post("/login", data={"password": "nope", "csrf_token": token})
    assert first.status_code == 401
    token = _csrf(first.text)
    second = client.post("/login", data={"password": "nope", "csrf_token": token})
    assert second.status_code == 401
    token = _csrf(second.text)
    blocked = client.post("/login", data={"password": "s3cret", "csrf_token": token})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "600"

    session_client = TestClient(create_app(load_events))
    _login(session_client)
    home = session_client.get("/")
    logged_out = session_client.post(
        "/logout",
        data={"csrf_token": _csrf(home.text)},
        follow_redirects=False,
    )
    assert logged_out.status_code == 303
    assert session_client.get("/", follow_redirects=False).status_code == 303
    _login(session_client)
    tampered = session_client.cookies.get("dashboard_session") + "x"
    session_client.cookies.set("dashboard_session", tampered)
    assert session_client.get("/", follow_redirects=False).status_code == 303
    session_client.cookies.set(
        "dashboard_session",
        seal_session(session_secret(), {"authenticated": True, "csrf": "x"}, ttl_seconds=-10),
    )
    assert session_client.get("/", follow_redirects=False).status_code == 303


def test_accounts_page_never_redisplays_the_private_key(monkeypatch, dashboard_env):
    from fastapi.testclient import TestClient

    from src.journal.web import create_app

    async def load_events():
        return []

    pem = _pem()
    secret_slice = _body_slice(pem)
    store = MemoryAccountStore(Fernet(Fernet.generate_key()))
    seen = {}

    async def probe(api_key_id, private_key):
        seen["api_key_id"] = api_key_id
        seen["private_key"] = private_key
        return "Connected. Cash balance $1.25."

    client = TestClient(create_app(load_events, account_store=store, probe=probe))
    _login(client)
    page = client.get("/accounts")
    assert page.status_code == 200
    token = _csrf(page.text)
    rejected = client.post(
        "/accounts",
        data={
            "csrf_token": token,
            "name": "bad",
            "api_key_id": "key-id-1",
            "private_key": "NOT-THE-KEY-MATERIAL-xyz",
        },
    )
    assert rejected.status_code == 400
    assert "could not be read" in rejected.text.lower()
    assert "NOT-THE-KEY-MATERIAL-xyz" not in rejected.text

    missing = client.post(
        "/accounts",
        data={"name": "main", "api_key_id": "key-id-1", "private_key": pem},
    )
    assert missing.status_code == 403
    assert store._rows == []

    token = _csrf(rejected.text)
    saved = client.post(
        "/accounts",
        data={
            "csrf_token": token,
            "name": "main",
            "api_key_id": "key-id-1",
            "private_key": pem,
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "key-id-1" in saved.text
    assert "ending " in saved.text
    assert secret_slice not in saved.text
    assert "BEGIN" not in saved.text
    assert "Dry run" in saved.text
    assert pem not in saved.text

    token = _csrf(saved.text)
    tested = client.post("/accounts/1/test", data={"csrf_token": token}, follow_redirects=True)
    assert "Connected. Cash balance $1.25." in tested.text
    assert seen["api_key_id"] == "key-id-1"
    assert secret_slice not in tested.text
    assert pem not in tested.text

    token = _csrf(tested.text)
    armed = client.post(
        "/accounts/1/live",
        data={"csrf_token": token, "live": "true"},
        follow_redirects=True,
    )
    assert "Live" in armed.text
    token = _csrf(armed.text)
    paused = client.post(
        "/accounts/1/enabled",
        data={"csrf_token": token, "enabled": "false"},
        follow_redirects=True,
    )
    assert "Disabled" in paused.text
    assert store._rows[0]["enabled"] is False

    token = _csrf(paused.text)
    gone = client.post("/accounts/1/delete", data={"csrf_token": token}, follow_redirects=True)
    assert "No stored accounts yet." in gone.text
    assert store._rows == []


def test_accounts_page_refuses_key_storage_without_encryption_key(monkeypatch, dashboard_env):
    from fastapi.testclient import TestClient

    from src.journal.web import create_app

    async def load_events():
        return []

    client = TestClient(create_app(load_events))
    _login(client)
    page = client.get("/accounts")
    assert page.status_code == 503
    assert "refusing to store" in page.text
    assert "<textarea" not in page.text


def test_cross_site_login_is_rejected(monkeypatch, dashboard_env):
    from fastapi.testclient import TestClient

    from src.journal.web import create_app

    async def load_events():
        return []

    client = TestClient(create_app(load_events))
    login = client.get("/login")
    token = _csrf(login.text)
    rejected = client.post(
        "/login",
        data={"password": "s3cret", "csrf_token": token},
        headers={"origin": "https://evil.example"},
    )
    assert rejected.status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 303
