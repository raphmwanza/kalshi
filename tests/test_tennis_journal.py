"""Journal, multi-account runner, late-round filter, and dashboard."""

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.clients.accounts import AccountConfigError, load_kalshi_accounts
from src.clients.kalshi_client import KalshiClient
from src.clients.private_key import load_kalshi_private_key
from src.clients.sofascore_client import is_semifinal_or_later, round_label_is_late
from src.journal.dashboard import render_dashboard
from src.journal.summary import summarize_events
from src.journal.tennis_log import JsonlJournal, SqliteJournal, journal_for_url
from src.strategies.tennis_runner import run_tennis_accounts
from src.strategies.tennis_set3 import TennisSet3Strategy
from tests.test_tennis_safety import FakeKalshi, FakeSofa, _alcaraz_event, _live_alcaraz


def test_round_filter_skips_semifinal_and_final_only():
    assert is_semifinal_or_later("Semifinals", None)
    assert is_semifinal_or_later("semi-final", None)
    assert is_semifinal_or_later("", 2)
    assert is_semifinal_or_later("Final", 1)
    assert is_semifinal_or_later("", 230)
    assert round_label_is_late("The Final")
    assert round_label_is_late("Wimbledon Men's Final")
    assert not is_semifinal_or_later("Quarterfinals", 4)
    assert not round_label_is_late("Quarter-final")
    assert not round_label_is_late("ATP Finals")
    assert not round_label_is_late("Round of 16")
    assert not is_semifinal_or_later("", None)


def test_summary_win_rate_pnl_and_implied_edge():
    events = [
        {
            "id": 1, "ts": "2026-01-01T00:00:00+00:00", "account": "main",
            "kind": "order", "order_action": "buy", "order_status": "executed",
            "ticker": "A", "rule": "set3_favorite", "price": 0.80, "implied_prob": 0.80,
            "fill_count": 2, "fees": 0.04, "size": 2,
        },
        {
            "id": 2, "ts": "2026-01-01T01:00:00+00:00", "account": "main",
            "kind": "order", "order_action": "buy", "order_status": "executed",
            "ticker": "B", "rule": "underdog", "price": 0.35, "implied_prob": 0.35,
            "fill_count": 4, "fees": 0.08, "size": 4,
        },
        {
            "id": 3, "ts": "2026-01-02T00:00:00+00:00", "account": "main",
            "kind": "settlement", "ticker": "A", "size": 2, "won": 1, "price": 1.0,
        },
        {
            "id": 4, "ts": "2026-01-02T01:00:00+00:00", "account": "main",
            "kind": "settlement", "ticker": "B", "size": 4, "won": 0, "price": 0.0,
        },
        {
            "id": 5, "ts": "2026-01-01T00:00:01+00:00", "account": "main",
            "kind": "signal", "decision": "skip", "rule": "set3_favorite",
        },
    ]
    summary = summarize_events(events)
    assert summary["closed"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["avg_entry_price"] == pytest.approx((0.80 + 0.35) / 2)
    # A: 2*1 - 2*0.80 - 0.04 = 0.36; B: 0 - 4*0.35 - 0.08 = -1.48
    assert summary["total_pnl"] == pytest.approx(0.36 - 1.48)
    assert summary["by_rule"]["set3_favorite"]["pnl"] == pytest.approx(0.36)
    assert summary["by_rule"]["underdog"]["pnl"] == pytest.approx(-1.48)
    # Settled win rate 0.5 vs average implied (0.80+0.35)/2 = 0.575, edge negative.
    assert summary["settled_win_rate"] == 0.5
    assert summary["avg_implied_prob"] == pytest.approx(0.575)
    assert summary["implied_edge"] == pytest.approx(0.5 - 0.575)
    assert summary["decisions"]["skip"] == 1
    page = render_dashboard(summary, events)
    assert "Settled edge" in page
    assert "set3_favorite" in page
    assert "underdog" in page


@pytest.mark.asyncio
async def test_sqlite_and_jsonl_journals_dedupe_and_reload(tmp_path):
    sqlite_journal = SqliteJournal(str(tmp_path / "journal.db"))
    event = {
        "ts": "2026-01-01T00:00:00+00:00",
        "account": "main",
        "kind": "order",
        "ticker": "KXATPMATCH-1",
        "rule": "set3_favorite",
        "player": "Carlos Alcaraz",
        "match_label": "Carlos Alcaraz vs Jannik Sinner",
        "score": "6-4 3-6 4-1",
        "side": "yes",
        "price": 0.8,
        "size": 1,
        "fees": 0.02,
        "client_order_id": "cid",
        "order_status": "executed",
        "fill_count": 1,
        "order_action": "buy",
        "implied_prob": 0.8,
        "dedupe_key": "order:main:cid",
    }
    await sqlite_journal.record(event)
    await sqlite_journal.record(event)
    rows = await sqlite_journal.load_events(account="main")
    await sqlite_journal.close()
    assert len(rows) == 1
    assert rows[0]["player"] == "Carlos Alcaraz"
    assert rows[0]["fees"] == pytest.approx(0.02)

    path = tmp_path / "journal.jsonl"
    jsonl = JsonlJournal(str(path))
    await jsonl.record(event)
    await jsonl.record(dict(event))
    loaded = await jsonl.load_events()
    assert len(loaded) == 1
    assert journal_for_url("postgresql://db.example/app").__class__.__name__ == "PostgresJournal"
    assert journal_for_url("sqlite:///tmp/x.db").path.endswith("tmp/x.db") or journal_for_url("sqlite:///tmp/x.db").path.endswith("tmp\\x.db")
    assert journal_for_url("", backend="jsonl", jsonl_path=str(path)).__class__.__name__ == "JsonlJournal"


def test_account_config_json_numbered_and_fallback(monkeypatch):
    monkeypatch.delenv("KALSHI_ACCOUNTS", raising=False)
    for index in range(1, 4):
        monkeypatch.delenv(f"KALSHI_API_KEY_ID_{index}", raising=False)
        monkeypatch.delenv(f"KALSHI_API_KEY_{index}", raising=False)
        monkeypatch.delenv(f"KALSHI_PRIVATE_KEY_{index}", raising=False)
        monkeypatch.delenv(f"KALSHI_ACCOUNT_NAME_{index}", raising=False)
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)

    monkeypatch.setenv(
        "KALSHI_ACCOUNTS",
        json.dumps([
            {"name": "main", "api_key_id": "key-1", "private_key": "-----BEGIN RSA PRIVATE KEY-----\\nABC\\n-----END RSA PRIVATE KEY-----"},
            {"name": "backup", "api_key": "key-2", "private_key": "-----BEGIN RSA PRIVATE KEY-----\nDEF\n-----END RSA PRIVATE KEY-----\n"},
        ]),
    )
    accounts = load_kalshi_accounts()
    assert [a.name for a in accounts] == ["main", "backup"]
    assert "\n" in accounts[0].private_key
    assert accounts[0].private_key.startswith("-----BEGIN")
    assert accounts[1].api_key == "key-2"

    monkeypatch.delenv("KALSHI_ACCOUNTS")
    monkeypatch.setenv("KALSHI_API_KEY_ID_1", "n1")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_1", "pem-1")
    monkeypatch.setenv("KALSHI_ACCOUNT_NAME_1", "one")
    monkeypatch.setenv("KALSHI_API_KEY_2", "n2")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_2", "pem-2")
    numbered = load_kalshi_accounts()
    assert [a.name for a in numbered] == ["one", "account-2"]
    assert numbered[1].api_key == "n2"

    monkeypatch.delenv("KALSHI_API_KEY_ID_1")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_1")
    monkeypatch.delenv("KALSHI_ACCOUNT_NAME_1")
    monkeypatch.delenv("KALSHI_API_KEY_2")
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_2")
    monkeypatch.setenv("KALSHI_API_KEY", "solo")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "solo-pem")
    solo = load_kalshi_accounts()
    assert solo[0].name == "default"
    assert solo[0].api_key == "solo"

    monkeypatch.setenv("KALSHI_ACCOUNTS", "{")
    with pytest.raises(AccountConfigError):
        load_kalshi_accounts()


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_explicit_pem_beats_the_environment(monkeypatch):
    first = _pem()
    second = _pem()
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", first)
    env_key, env_source = load_kalshi_private_key()
    explicit, source = load_kalshi_private_key(pem=second)
    assert env_source == "KALSHI_PRIVATE_KEY"
    assert source == "explicit"
    assert explicit.private_numbers().d != env_key.private_numbers().d
    client = KalshiClient(api_key="account-b", private_key_pem=second)
    try:
        assert client.api_key == "account-b"
        assert client.private_key.private_numbers().d == explicit.private_numbers().d
    finally:
        # close is async; the HTTP client can be dropped in this sync test
        pass


@pytest.mark.asyncio
async def test_runner_isolates_account_errors_and_fetches_sofascore_once():
    class CountingSofa(FakeSofa):
        def __init__(self):
            super().__init__([_live_alcaraz()])
            self.fetches = 0

        async def get_live_tennis_matches(self):
            self.fetches += 1
            return await super().get_live_tennis_matches()

    from src.clients.accounts import KalshiAccount

    sofa = CountingSofa()
    good = FakeKalshi()
    good.events = [_alcaraz_event()]
    good.balance = {"balance": 82, "portfolio_value": 0}

    def factory(account):
        if account.name == "bad":
            raise RuntimeError("key rejected")
        return good

    stats = await run_tennis_accounts(
        [KalshiAccount(name="bad", api_key="x", private_key="y"), KalshiAccount(name="good", api_key="x", private_key="y")],
        sofascore=sofa,
        live=True,
        client_factory=factory,
    )
    assert sofa.fetches == 1
    assert stats["account_errors"] == 1
    assert stats["needs_backoff"] is True
    assert stats["by_account"]["good"]["placed"] == 1
    assert good.placed


@pytest.mark.asyncio
async def test_strategy_logs_entries_and_skips_semifinals(tmp_path):
    journal = SqliteJournal(str(tmp_path / "j.db"))
    client = FakeKalshi()
    client.balance = {"balance": 82, "portfolio_value": 0}
    client.events = [_alcaraz_event()]
    match = _live_alcaraz()
    strategy = TennisSet3Strategy(
        client=client,
        sofascore=FakeSofa([match]),
        dry_run=False,
        account_name="main",
        journal=journal,
    )
    stats = await strategy.run()
    rows = await journal.load_events(account="main")
    kinds = [row["kind"] for row in rows]
    assert "signal" in kinds
    assert "order" in kinds
    buy = next(row for row in rows if row["kind"] == "order" and row["order_action"] == "buy")
    assert buy["rule"] == "set3_favorite"
    assert buy["account"] == "main"
    assert buy["fill_count"] == 1
    assert buy["score"]
    assert stats["placed"] == 1

    client.placed.clear()
    late = _live_alcaraz()
    late.round_name = "Semifinal"
    late.cup_round_type = 2
    skipped = await strategy.run(live_matches=[late])
    assert skipped["skipped_late_round"] == 2
    assert client.placed == []
    await journal.close()
