"""Encrypted Kalshi keys pasted in the dashboard.

Keys live in Postgres, ciphertext only, using Fernet and KEYS_ENCRYPTION_KEY.
The store refuses to open when that variable is missing or not a Fernet key,
and when DATABASE_URL is not Postgres. Plaintext private keys are never logged
and never returned by the list used to render the page.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization

from src.clients.accounts import (
    KalshiAccount,
    env_has_explicit_api_key,
    is_unconfigured_default,
    load_kalshi_accounts,
    merge_accounts,
)
from src.clients.private_key import normalize_pem_env

logger = logging.getLogger(__name__)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_MAX_PEM_CHARS = 100_000
_notice_logged = False


class AccountStoreUnavailable(RuntimeError):
    """Key storage must not start. The message is safe to show and to log."""


class AccountStoreError(ValueError):
    """A rejected account form. The message does not contain key material."""


@dataclass(frozen=True)
class AccountRecord:
    """Public view of a stored account. No private key."""

    id: int
    name: str
    api_key_id: str
    key_hint: str
    enabled: bool
    live: bool


def fernet_from_env() -> Fernet:
    raw = os.environ.get("KEYS_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise AccountStoreUnavailable(
            "KEYS_ENCRYPTION_KEY is not set; refusing to store Kalshi keys"
        )
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception:
        raise AccountStoreUnavailable(
            "KEYS_ENCRYPTION_KEY is not a valid Fernet key; refusing to store Kalshi keys"
        ) from None


def _is_postgres(url: str) -> bool:
    return urlparse(url).scheme.lower() in {"postgres", "postgresql"}


def private_key_hint(pem: str) -> str:
    """Last four characters of the PEM body, never the header."""
    lines = [
        line.strip()
        for line in (pem or "").splitlines()
        if line.strip() and "PRIVATE KEY" not in line
    ]
    compact = "".join(lines)
    if len(compact) < 4:
        return ""
    return compact[-4:]


def validate_account_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not _NAME.match(cleaned):
        raise AccountStoreError(
            "Name must be 1–40 letters, numbers, dashes, or underscores."
        )
    return cleaned


def validate_api_key_id(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned or len(cleaned) > 200 or any(ch.isspace() for ch in cleaned):
        raise AccountStoreError("API key ID is missing or not a single token.")
    return cleaned


def validate_private_key(pem: str) -> str:
    if pem is None or len(pem) > _MAX_PEM_CHARS:
        raise AccountStoreError("Private key was rejected.")
    normalized = normalize_pem_env(pem)
    try:
        serialization.load_pem_private_key(normalized.encode("utf-8"), password=None)
    except Exception:
        raise AccountStoreError(
            "Private key could not be read. Paste the unencrypted PEM."
        ) from None
    return normalized


def encrypt_private_key(fernet: Fernet, pem: str) -> str:
    return fernet.encrypt(pem.encode("utf-8")).decode("ascii")


def decrypt_private_key(fernet: Fernet, token: str) -> str:
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        raise AccountStoreError("Stored key could not be decrypted.") from None


class MemoryAccountStore:
    """Fernet-backed store for tests. Same rules as Postgres, no network."""

    def __init__(self, fernet: Optional[Fernet]):
        if fernet is None:
            raise AccountStoreUnavailable(
                "KEYS_ENCRYPTION_KEY is not set; refusing to store Kalshi keys"
            )
        self._fernet = fernet
        self._rows: List[dict] = []
        self._next_id = 1

    async def ensure_schema(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_accounts(self) -> List[AccountRecord]:
        return [
            AccountRecord(
                id=row["id"],
                name=row["name"],
                api_key_id=row["api_key_id"],
                key_hint=row["key_hint"],
                enabled=row["enabled"],
                live=row["live"],
            )
            for row in self._rows
        ]

    async def add_account(self, name: str, api_key_id: str, pem: str) -> AccountRecord:
        name = validate_account_name(name)
        api_key_id = validate_api_key_id(api_key_id)
        pem = validate_private_key(pem)
        if any(row["name"] == name for row in self._rows):
            raise AccountStoreError("An account with that name already exists.")
        record_id = self._next_id
        self._next_id += 1
        self._rows.append({
            "id": record_id,
            "name": name,
            "api_key_id": api_key_id,
            "key_hint": private_key_hint(pem),
            "private_key_encrypted": encrypt_private_key(self._fernet, pem),
            "enabled": True,
            "live": False,
        })
        listed = await self.list_accounts()
        return next(row for row in listed if row.id == record_id)

    async def set_enabled(self, account_id: int, enabled: bool) -> None:
        row = self._find(account_id)
        row["enabled"] = bool(enabled)

    async def set_live(self, account_id: int, live: bool) -> None:
        row = self._find(account_id)
        row["live"] = bool(live)

    async def delete_account(self, account_id: int) -> None:
        self._find(account_id)
        self._rows = [row for row in self._rows if row["id"] != account_id]

    async def load_secret(self, account_id: int) -> tuple:
        row = self._find(account_id)
        pem = decrypt_private_key(self._fernet, row["private_key_encrypted"])
        return row["api_key_id"], pem

    async def enabled_accounts(self) -> List[KalshiAccount]:
        accounts = []
        for row in self._rows:
            if not row["enabled"]:
                continue
            try:
                pem = decrypt_private_key(self._fernet, row["private_key_encrypted"])
            except AccountStoreError:
                logger.error("Could not decrypt stored key for account %s", row["name"])
                continue
            accounts.append(KalshiAccount(
                name=row["name"],
                api_key=row["api_key_id"],
                private_key=pem,
                live=bool(row["live"]),
            ))
        return accounts

    def _find(self, account_id: int) -> dict:
        for row in self._rows:
            if row["id"] == account_id:
                return row
        raise AccountStoreError("No account with that id.")


class PostgresAccountStore:
    """Postgres ciphertext table. Opens only after the Fernet key is accepted."""

    def __init__(self, dsn: str, fernet: Fernet):
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]
        self.dsn = dsn
        self._fernet = fernet
        self._conn = None

    async def ensure_schema(self) -> None:
        if self._conn is None:
            import asyncpg

            self._conn = await asyncpg.connect(self.dsn)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_kalshi_accounts (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                api_key_id TEXT NOT NULL,
                key_hint TEXT NOT NULL,
                private_key_encrypted TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                live BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def list_accounts(self) -> List[AccountRecord]:
        rows = await self._conn.fetch(
            """
            SELECT id, name, api_key_id, key_hint, enabled, live
            FROM dashboard_kalshi_accounts
            ORDER BY id
            """
        )
        return [
            AccountRecord(
                id=int(row["id"]),
                name=row["name"],
                api_key_id=row["api_key_id"],
                key_hint=row["key_hint"],
                enabled=bool(row["enabled"]),
                live=bool(row["live"]),
            )
            for row in rows
        ]

    async def add_account(self, name: str, api_key_id: str, pem: str) -> AccountRecord:
        name = validate_account_name(name)
        api_key_id = validate_api_key_id(api_key_id)
        pem = validate_private_key(pem)
        hint = private_key_hint(pem)
        token = encrypt_private_key(self._fernet, pem)
        try:
            row = await self._conn.fetchrow(
                """
                INSERT INTO dashboard_kalshi_accounts
                    (name, api_key_id, key_hint, private_key_encrypted, enabled, live)
                VALUES ($1, $2, $3, $4, TRUE, FALSE)
                RETURNING id, name, api_key_id, key_hint, enabled, live
                """,
                name,
                api_key_id,
                hint,
                token,
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise AccountStoreError("An account with that name already exists.") from None
            logger.error("Could not save Kalshi account %s", name)
            raise AccountStoreError("Could not save the account.") from None
        return AccountRecord(
            id=int(row["id"]),
            name=row["name"],
            api_key_id=row["api_key_id"],
            key_hint=row["key_hint"],
            enabled=bool(row["enabled"]),
            live=bool(row["live"]),
        )

    async def set_enabled(self, account_id: int, enabled: bool) -> None:
        await self._update_flag(account_id, "enabled", bool(enabled))

    async def set_live(self, account_id: int, live: bool) -> None:
        await self._update_flag(account_id, "live", bool(live))

    async def _update_flag(self, account_id: int, column: str, value: bool) -> None:
        if column not in {"enabled", "live"}:
            raise AccountStoreError("No account with that id.")
        result = await self._conn.execute(
            f"UPDATE dashboard_kalshi_accounts SET {column} = $2 WHERE id = $1",
            account_id,
            value,
        )
        if result == "UPDATE 0":
            raise AccountStoreError("No account with that id.")

    async def delete_account(self, account_id: int) -> None:
        result = await self._conn.execute(
            "DELETE FROM dashboard_kalshi_accounts WHERE id = $1",
            account_id,
        )
        if result == "DELETE 0":
            raise AccountStoreError("No account with that id.")

    async def load_secret(self, account_id: int) -> tuple:
        row = await self._conn.fetchrow(
            """
            SELECT api_key_id, private_key_encrypted
            FROM dashboard_kalshi_accounts
            WHERE id = $1
            """,
            account_id,
        )
        if row is None:
            raise AccountStoreError("No account with that id.")
        pem = decrypt_private_key(self._fernet, row["private_key_encrypted"])
        return row["api_key_id"], pem

    async def enabled_accounts(self) -> List[KalshiAccount]:
        rows = await self._conn.fetch(
            """
            SELECT name, api_key_id, private_key_encrypted, live
            FROM dashboard_kalshi_accounts
            WHERE enabled = TRUE
            ORDER BY id
            """
        )
        accounts = []
        for row in rows:
            try:
                pem = decrypt_private_key(self._fernet, row["private_key_encrypted"])
            except AccountStoreError:
                logger.error("Could not decrypt stored key for account %s", row["name"])
                continue
            accounts.append(KalshiAccount(
                name=row["name"],
                api_key=row["api_key_id"],
                private_key=pem,
                live=bool(row["live"]),
            ))
        return accounts


def open_account_store() -> PostgresAccountStore:
    """Open key storage, or raise AccountStoreUnavailable before touching the database."""
    fernet = fernet_from_env()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url or not _is_postgres(url):
        raise AccountStoreUnavailable(
            "Kalshi key storage requires a Postgres DATABASE_URL"
        )
    return PostgresAccountStore(url, fernet)


def _notice_once(message: str) -> None:
    global _notice_logged
    if _notice_logged:
        return
    _notice_logged = True
    logger.info("%s", message)


async def load_cycle_accounts(
    *,
    open_store=open_account_store,
) -> List[KalshiAccount]:
    """Env accounts plus enabled database accounts. Safe to call every cycle.

    A missing encryption key or a non-Postgres URL skips the database and
    leaves the env accounts running. A database error does the same without
    logging the connection string or any key.
    """
    env_accounts = load_kalshi_accounts()
    try:
        store = open_store()
    except AccountStoreUnavailable as exc:
        _notice_once(str(exc))
        return env_accounts
    try:
        await store.ensure_schema()
        stored = await store.enabled_accounts()
    except Exception:
        logger.error(
            "Could not load stored Kalshi accounts; continuing with environment accounts only"
        )
        return env_accounts
    finally:
        await store.close()
    # The image sets KALSHI_PRIVATE_KEY_PATH even when no key file exists.
    # That path-only "default" account is not a configured env account. Drop
    # it when the dashboard already supplied accounts and no API key id is set.
    if stored and not env_has_explicit_api_key():
        env_accounts = [
            account
            for account in env_accounts
            if account.api_key.strip() or account.private_key.strip()
        ]
        env_accounts = [
            account for account in env_accounts if not is_unconfigured_default(account)
        ]
    elif stored and all(is_unconfigured_default(account) for account in env_accounts):
        env_accounts = []
    return merge_accounts(env_accounts, stored)


def reset_store_notice() -> None:
    """Test helper so one process can observe the notice more than once."""
    global _notice_logged
    _notice_logged = False
