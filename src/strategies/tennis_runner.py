"""Run the tennis strategy once per Kalshi account.

SofaScore is fetched once per cycle and reused. Each account has its own
Kalshi client, bankroll, position cap, and journal tag. An error in one
account is logged and the others still run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from src.clients.accounts import KalshiAccount
from src.clients.kalshi_client import KalshiClient
from src.strategies.tennis_set3 import TennisSet3Strategy

logger = logging.getLogger(__name__)


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

    for account in accounts:
        combined["accounts"] += 1
        print(f"\n👤 Account {account.name}", flush=True)
        client = None
        try:
            client = client_factory(account)
            strategy = TennisSet3Strategy(
                client=client,
                sofascore=sofascore,
                dry_run=not live,
                account_name=account.name,
                journal=journal,
            )
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
            combined["by_account"][account.name] = {"error": str(exc), "needs_backoff": True}
            logger.error("Account %s failed; continuing with the others: %s", account.name, exc)
            print(f"  ❌ Account {account.name} failed: {exc}. Continuing.", flush=True)
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Closing account %s failed: %s", account.name, exc)
            await _record_cycle(journal, account.name, combined["by_account"].get(account.name))
    return combined


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
