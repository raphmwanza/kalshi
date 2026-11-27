"""Load one or more Kalshi accounts from the environment.

Priority:
1. ``KALSHI_ACCOUNTS`` — a JSON list of ``{name, api_key_id, private_key}``.
2. Numbered pairs ``KALSHI_API_KEY_ID_1`` / ``KALSHI_PRIVATE_KEY_1``, then ``_2``,
   stopping at the first gap. ``KALSHI_API_KEY_1`` is accepted as an alias.
3. The original single-account variables (``KALSHI_API_KEY`` plus
   ``KALSHI_PRIVATE_KEY`` or ``KALSHI_PRIVATE_KEY_PATH``).

An empty result still yields one ``default`` account so the existing file-based
key keeps working.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from src.clients.private_key import normalize_pem_env

logger = logging.getLogger(__name__)


class AccountConfigError(ValueError):
    """The account environment variables are present but unusable."""


@dataclass(frozen=True)
class KalshiAccount:
    name: str
    api_key: str = ""
    private_key: str = ""
    private_key_path: str = ""
    # None follows the process live flag (env accounts). False is dry run.
    # True may place real orders only when the process itself is live.
    live: Optional[bool] = None


def _pem(value: str) -> str:
    if not value or not value.strip():
        return ""
    return normalize_pem_env(value)


def _from_json(raw: str) -> list[KalshiAccount]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AccountConfigError(f"KALSHI_ACCOUNTS is not valid JSON: {exc}") from exc
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        raise AccountConfigError("KALSHI_ACCOUNTS must be a non-empty JSON list")
    accounts = []
    seen = set()
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise AccountConfigError(f"KALSHI_ACCOUNTS[{index}] is not an object")
        name = str(item.get("name") or f"account-{index}").strip()
        if name in seen:
            raise AccountConfigError(f"Duplicate Kalshi account name {name!r}")
        seen.add(name)
        api_key = str(item.get("api_key_id") or item.get("api_key") or "").strip()
        private_key = _pem(str(item.get("private_key") or ""))
        private_key_path = str(item.get("private_key_path") or "").strip()
        if not api_key:
            raise AccountConfigError(f"Account {name!r} is missing api_key_id")
        if not private_key and not private_key_path:
            raise AccountConfigError(f"Account {name!r} is missing private_key")
        accounts.append(KalshiAccount(
            name=name,
            api_key=api_key,
            private_key=private_key,
            private_key_path=private_key_path,
        ))
    return accounts


def _from_numbered() -> list[KalshiAccount]:
    accounts = []
    index = 1
    while True:
        api_key = (
            os.environ.get(f"KALSHI_API_KEY_ID_{index}", "").strip()
            or os.environ.get(f"KALSHI_API_KEY_{index}", "").strip()
        )
        private_key = os.environ.get(f"KALSHI_PRIVATE_KEY_{index}", "")
        private_key_path = os.environ.get(f"KALSHI_PRIVATE_KEY_PATH_{index}", "").strip()
        name = os.environ.get(f"KALSHI_ACCOUNT_NAME_{index}", "").strip()
        if not api_key and not private_key.strip() and not private_key_path and not name:
            break
        name = name or f"account-{index}"
        if not api_key or not (private_key.strip() or private_key_path):
            raise AccountConfigError(
                f"KALSHI account {index} needs both an API key id and a private key"
            )
        accounts.append(KalshiAccount(
            name=name,
            api_key=api_key,
            private_key=_pem(private_key),
            private_key_path=private_key_path,
        ))
        index += 1
        if index > 50:
            break
    return accounts


def _from_single() -> list[KalshiAccount]:
    api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    name = os.environ.get("KALSHI_ACCOUNT_NAME", "").strip() or "default"
    if not api_key and not private_key.strip() and not private_key_path:
        return []
    return [KalshiAccount(
        name=name,
        api_key=api_key,
        private_key=_pem(private_key),
        private_key_path=private_key_path,
    )]


def redact_key_material(text: str) -> str:
    """Drop exception text that contains a PEM header."""
    if "-----BEGIN" in text or "-----END" in text:
        return "account key error"
    return text


def account_is_live(account: KalshiAccount, process_live: bool) -> bool:
    """Whether this account may place real orders this cycle.

    A dry-run worker stays dry for every account. Env accounts follow the
    process flag. A stored account stays on dry run unless its own toggle is
    on, so a key pasted in the dashboard does not trade until that toggle
    is switched and the worker is live.
    """
    if not process_live:
        return False
    if account.live is None:
        return True
    return bool(account.live)


def is_unconfigured_default(account: KalshiAccount) -> bool:
    """True for the placeholder returned when no account env vars are set."""
    return (
        account.name == "default"
        and not account.api_key
        and not account.private_key
        and not account.private_key_path
        and account.live is None
    )


def merge_accounts(
    env_accounts: List[KalshiAccount],
    stored_accounts: List[KalshiAccount],
) -> List[KalshiAccount]:
    """Env accounts first. A stored account with the same name is skipped."""
    merged: List[KalshiAccount] = []
    seen = set()
    for account in list(env_accounts) + list(stored_accounts):
        if account.name in seen:
            logger.warning(
                "Skipping duplicate Kalshi account name %s; the environment account is kept",
                account.name,
            )
            continue
        seen.add(account.name)
        merged.append(account)
    return merged


def load_kalshi_accounts() -> list[KalshiAccount]:
    """Accounts configured for this process, defaulting to one unnamed account."""
    raw = os.environ.get("KALSHI_ACCOUNTS", "").strip()
    if raw:
        return _from_json(raw)
    numbered = _from_numbered()
    if numbered:
        return numbered
    single = _from_single()
    if single:
        return single
    return [KalshiAccount(name="default")]
