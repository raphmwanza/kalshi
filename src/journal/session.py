"""Signed session cookies, CSRF tokens, and login rate limiting.

The cookie is HMAC-SHA256 signed with a key derived from DASHBOARD_PASSWORD.
It is HttpOnly and SameSite=Strict. The Secure flag is on unless
DASHBOARD_COOKIE_SECURE is explicitly false (local http only).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Dict

from fastapi import Request
from fastapi.responses import Response

SESSION_COOKIE = "dashboard_session"
SESSION_TTL_SECONDS = 12 * 60 * 60


def dashboard_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def cookie_secure() -> bool:
    raw = os.environ.get("DASHBOARD_COOKIE_SECURE", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def session_secret() -> str:
    """Stable signing key. Changing the password invalidates existing sessions."""
    password = dashboard_password()
    material = password if password else "dashboard-unconfigured"
    return hashlib.sha256(f"kalshi-dashboard-session\n{material}".encode()).hexdigest()


def password_matches(provided: str) -> bool:
    configured = dashboard_password()
    if not configured:
        return False
    expected = hashlib.sha256(configured.encode()).digest()
    actual = hashlib.sha256((provided or "").encode()).digest()
    return secrets.compare_digest(expected, actual)


def seal_session(secret: str, data: Dict, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    payload = {
        "authenticated": bool(data.get("authenticated")),
        "csrf": str(data.get("csrf") or ""),
        "flash": str(data.get("flash") or "")[:300],
        "exp": int(time.time()) + int(ttl_seconds),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def open_session(secret: str, token: str) -> Dict:
    if not token or "." not in token:
        return {}
    body, mac = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return {}
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        expires = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return {}
    if expires < time.time():
        return {}
    return payload


def read_session(request: Request) -> Dict:
    token = request.cookies.get(SESSION_COOKIE) or ""
    return open_session(session_secret(), token)


def write_session_cookie(response: Response, data: Dict, *, secure: bool | None = None) -> None:
    token = seal_session(session_secret(), data)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=cookie_secure() if secure is None else secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        secure=cookie_secure(),
        httponly=True,
        samesite="strict",
    )


def ensure_csrf(session: Dict) -> str:
    token = str(session.get("csrf") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_ok(session: Dict, form_token: str) -> bool:
    expected = str(session.get("csrf") or "")
    provided = str(form_token or "")
    if not expected or not provided:
        return False
    return secrets.compare_digest(expected, provided)


def origin_ok(request: Request) -> bool:
    """Reject a cross-site POST when the browser sent Origin."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host") or ""
    if not host:
        return False
    return origin == f"https://{host}" or origin == f"http://{host}"


def client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class LoginLimiter:
    """In-memory failure window. One dashboard process is enough for a single owner."""

    def __init__(self, max_failures: int = 8, window_seconds: float = 600):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: Dict[str, list] = {}

    def _fresh(self, ip: str, now: float) -> list:
        stamps = [stamp for stamp in self._failures.get(ip, []) if now - stamp < self.window_seconds]
        self._failures[ip] = stamps
        return stamps

    def allowed(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return len(self._fresh(ip, now)) < self.max_failures

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._fresh(ip, now)
        self._failures.setdefault(ip, []).append(now)

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
