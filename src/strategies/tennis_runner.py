"""Run the tennis strategy once per Kalshi account.

SofaScore is fetched once per cycle and reused. Each account has its own
Kalshi client, bankroll, position cap, and journal tag. An error in one
account is logged and the others still run.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.clients.accounts import KalshiAccount, account_is_live, redact_key_material
from src.clients.kalshi_client import KalshiClient
from src.strategies.tennis_set3 import TennisSet3Strategy

logger = logging.getLogger(__name__)

# Survives across cycles in this process. A new strategy (and therefore a
# reset sync timer) is built only when the account name or key material changes.
_SESSIONS: Dict[Tuple[str, str, str], TennisSet3Strategy] = {}


def reset_account_sessions() -> None:
    """Drop cached strategies. Tests use this so one case cannot leak into the next."""
    _SESSIONS.clear()


def _session_key(account: KalshiAccount) -> Tuple[str, str, str]:
    """Name, key id, and a hash of the secret. The PEM itself is not stored."""
    secret = account.private_key or ""
    path = account.private_key_path or ""
    digest = hashlib.sha256(f"{secret}\n{path}".encode()).hexdigest()
    return (account.name, account.api_key or "", digest)


async def _close_strategy(strategy: TennisSet3Strategy) -> None:
    client = getattr(strategy, "client", None)
    inner = getattr(client, "_inner", client)
    close = getattr(inner, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Closing a replaced Kalshi client failed: %s", type(exc).__name__)


def client_for_account(account: KalshiAccount) -> KalshiClient:
    """Build a client that does not read another account's PEM from the environment."""
    kwargs = {}
    if account.api_key:
        kwargs["api_key"] = account.api_key
    if account.private_key:
        kwargs["private_key_pem"] = account.private_key
    elif account.private_key_path:
        kwargs["private_key_path"] = account.private_key_path
    return KalshiClient(**kwargs)


async def run_tennis_accounts(
    accounts: List[KalshiAccount],
    *,
    sofascore,
    journal=None,
    live: bool = False,
    matches: Optional[list] = None,
    client_factory=client_for_account,
) -> dict:
    """Run every account against one SofaScore snapshot.

    Returns a combined stats dict. ``needs_backoff`` is true when SofaScore
    was blocked or any account asked for a backoff or raised.
    """
    if matches is None:
        matches = await sofascore.get_live_tennis_matches()
    blocked = bool(getattr(sofascore, "last_fetch_blocked", False))
    combined = {
        "needs_backoff": blocked,
        "accounts": 0,
        "account_errors": 0,
        "errors": 0,
        "placed": 0,
        "by_account": {},
    }
    if not accounts:
        accounts = [KalshiAccount(name="default")]

    active_keys = set()
    for account in accounts:
        combined["accounts"] += 1
        account_live = account_is_live(account, live)
        mode = "live" if account_live else "dry run"
        print(f"\n👤 Account {account.name} ({mode})", flush=True)
        key = _session_key(account)
        active_keys.add(key)
        try:
            strategy = await _strategy_for_cycle(
                account,
                key,
                sofascore=sofascore,
                journal=journal,
                dry_run=not account_live,
                client_factory=client_factory,
                matches=matches,
            )
            if strategy is None:
                stats = {
                    "placed": 0,
                    "errors": 0,
                    "kalshi_calls": 0,
                    "actionable_matches": 0,
                    "live_matches": len(matches or []),
                    "needs_backoff": blocked,
                }
                print("scores only, no Kalshi calls", flush=True)
                logger.info("scores only, no Kalshi calls")
            else:
                stats = await strategy.run(live_matches=matches)
            combined["by_account"][account.name] = stats
            combined["placed"] += int(stats.get("placed") or 0)
            combined["errors"] += int(stats.get("errors") or 0)
            if stats.get("needs_backoff"):
                combined["needs_backoff"] = True
        except Exception as exc:  # noqa: BLE001 - one account must not stop the rest
            combined["account_errors"] += 1
            combined["errors"] += 1
            combined["needs_backoff"] = True
            safe_error = redact_key_material(str(exc))
            combined["by_account"][account.name] = {"error": safe_error, "needs_backoff": True}
            logger.error("Account %s failed; continuing with the others: %s", account.name, safe_error)
            print(f"  ❌ Account {account.name} failed: {safe_error}. Continuing.", flush=True)
        finally:
            await _record_cycle(journal, account.name, combined["by_account"].get(account.name))
    await _drop_retired_sessions(active_keys)
    return combined


class _ScoreOnly:
    """No network. Lets the entry rules run before a Kalshi client exists."""

    last_fetch_blocked = False


_PROBE: Optional[TennisSet3Strategy] = None


def _rules() -> TennisSet3Strategy:
    """One strategy object used only to classify scores. It is never cached."""
    global _PROBE
    if _PROBE is None:
        _PROBE = TennisSet3Strategy(
            client=_NullClient(),
            sofascore=_ScoreOnly(),
            account_name="_probe",
        )
    return _PROBE


def _matches_need_kalshi(matches: Optional[list]) -> bool:
    """True when any score in this snapshot can open a new trade.

    A fresh process still builds a client once, so an open position is seen
    at startup. Later idle cycles do not.
    """
    if not matches:
        return False
    rules = _rules()
    return any(rules._match_actionable(match) for match in matches)


async def _strategy_for_cycle(
    account: KalshiAccount,
    key: Tuple[str, str, str],
    *,
    sofascore,
    journal,
    dry_run: bool,
    client_factory,
    matches: Optional[list],
) -> Optional[TennisSet3Strategy]:
    """Reuse the account's strategy. Create a client only when Kalshi is required."""
    current = _SESSIONS.get(key)
    if current is not None:
        current.dry_run = dry_run
        current.journal = journal
        current.sofascore = sofascore
        current._owns_sofascore = False
        if (
            not _matches_need_kalshi(matches)
            and not current._sync_due()
            and not current._has_cached_tennis_exposure()
        ):
            return None
        return current

    # Same name, different key: the old client must not keep signing.
    for old_key, old in list(_SESSIONS.items()):
        if old_key[0] == account.name and old_key != key:
            await _close_strategy(old)
            _SESSIONS.pop(old_key, None)

    client = client_factory(account)
    strategy = TennisSet3Strategy(
        client=client,
        sofascore=sofascore,
        dry_run=dry_run,
        account_name=account.name,
        journal=journal,
    )
    _SESSIONS[key] = strategy
    return strategy


async def _drop_retired_sessions(active_keys: set) -> None:
    for key, strategy in list(_SESSIONS.items()):
        if key in active_keys:
            continue
        await _close_strategy(strategy)
        _SESSIONS.pop(key, None)


class _NullClient:
    """Stand-in so the score rules can run before any Kalshi client exists."""

    def __getattr__(self, name):
        raise RuntimeError("Kalshi client is not created on an idle score cycle")


async def _record_cycle(journal, account_name: str, stats: Optional[dict]) -> None:
    """Append a heartbeat the read-only dashboard can show as last cycle time."""
    if journal is None:
        return
    stats = stats or {}
    if stats.get("error"):
        status = "error"
    elif stats.get("needs_backoff"):
        status = "backoff"
    else:
        status = "ok"
    detail = str(stats.get("error") or "")
    if not detail:
        detail = (
            f"placed={int(stats.get('placed') or 0)} "
            f"errors={int(stats.get('errors') or 0)}"
        )
    ts = datetime.now(timezone.utc).isoformat()
    try:
        await journal.record({
            "ts": ts,
            "account": account_name,
            "kind": "cycle",
            "order_status": status,
            "details": detail[:500],
            "dedupe_key": f"cycle:{account_name}:{ts}",
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record cycle heartbeat for %s: %s", account_name, exc)
