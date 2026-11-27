"""Tennis journal and Kalshi account dashboard.

Run with ``BOT_MODE=dashboard`` or ``python -m src.journal.web``. It listens
on ``$PORT`` (default 8080). The journal page does not place orders. The
Accounts page stores API keys only when KEYS_ENCRYPTION_KEY and a Postgres
DATABASE_URL are both set.

Login uses a signed HttpOnly session cookie, a CSRF token on every form,
and a per-IP failure limit. DASHBOARD_PASSWORD is required.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.journal.account_store import (
    AccountStoreError,
    AccountStoreUnavailable,
    open_account_store,
)
from src.journal.accounts_page import render_accounts_page, render_login_page
from src.journal.dashboard import render_dashboard
from src.journal.session import (
    LoginLimiter,
    clear_session_cookie,
    client_ip,
    cookie_secure,
    csrf_ok,
    dashboard_password,
    ensure_csrf,
    origin_ok,
    password_matches,
    read_session,
    write_session_cookie,
)
from src.journal.summary import summarize_events

logger = logging.getLogger(__name__)


def _nostore(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _finish(response, session: dict):
    _nostore(response)
    if session.get("_drop"):
        clear_session_cookie(response)
        return response
    write_session_cookie(response, session, secure=cookie_secure())
    return response


def _unconfigured() -> HTMLResponse:
    return _nostore(HTMLResponse(
        "Set DASHBOARD_PASSWORD before exposing this dashboard.",
        status_code=503,
    ))


async def _load_events() -> list:
    from src.journal.tennis_log import open_journal

    journal = await open_journal()
    try:
        return await journal.load_events()
    finally:
        await journal.close()


async def probe_balance(api_key_id: str, pem: str) -> str:
    """Read-only balance check. The result text never includes the key."""
    from src.clients.kalshi_client import KalshiClient

    client = KalshiClient(api_key=api_key_id, private_key_pem=pem)
    try:
        payload = await client.get_balance()
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        label = type(exc).__name__ if code is None else f"{type(exc).__name__} {code}"
        logger.info("Kalshi balance check failed (%s)", label)
        return f"Connection failed ({label})."
    finally:
        await client.close()
    raw = payload.get("balance") if isinstance(payload, dict) else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return f"Connected. Cash balance ${float(raw) / 100:.2f}."
    return "Connected. The balance response had no cash figure."


def create_app(
    load_events=None,
    account_store=None,
    probe=None,
    login_limiter: LoginLimiter | None = None,
) -> FastAPI:
    """Build the dashboard. Tests can inject the loader, store, probe, and limiter."""
    loader = load_events or _load_events
    balance_probe = probe or probe_balance
    limiter = login_limiter or LoginLimiter()
    app = FastAPI(title="Tennis journal", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.account_store = account_store
    app.state.login_limiter = limiter

    def _session(request: Request) -> dict:
        data = read_session(request)
        data.pop("exp", None)
        return data

    async def _use_store():
        if app.state.account_store is not None:
            return app.state.account_store, False
        store = open_account_store()
        try:
            await store.ensure_schema()
        except AccountStoreUnavailable:
            await store.close()
            raise
        except Exception:
            await store.close()
            logger.error("Could not open Kalshi key storage")
            raise AccountStoreUnavailable(
                "Kalshi key storage could not be opened."
            ) from None
        return store, True

    async def _accounts_response(
        session: dict,
        *,
        flash: str = "",
        error: str = "",
        unavailable: str = "",
        status_code: int = 200,
        store=None,
    ):
        token = ensure_csrf(session)
        close_store = False
        accounts = []
        if not unavailable:
            try:
                if store is None:
                    store, close_store = await _use_store()
                accounts = await store.list_accounts()
            except AccountStoreUnavailable as exc:
                unavailable = str(exc)
                if status_code == 200:
                    status_code = 503
            finally:
                if close_store and store is not None:
                    await store.close()
        page = render_accounts_page(
            accounts,
            csrf_token=token,
            flash=flash,
            error=error,
            unavailable=unavailable,
        )
        return _finish(HTMLResponse(page, status_code=status_code), session)

    async def _authorized_form(request: Request, session: dict):
        if not dashboard_password():
            return _unconfigured(), None
        if not session.get("authenticated"):
            return _nostore(JSONResponse(status_code=401, content={"detail": "Login required"})), None
        if not origin_ok(request):
            return _nostore(HTMLResponse("Cross-site request rejected.", status_code=403)), None
        form = await request.form()
        if not csrf_ok(session, str(form.get("csrf_token") or "")):
            response = await _accounts_response(
                session,
                error="Form expired. Try again.",
                status_code=403,
            )
            return response, None
        return None, form

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        if not dashboard_password():
            return _unconfigured()
        session = _session(request)
        if session.get("authenticated"):
            return _nostore(RedirectResponse("/", status_code=303))
        token = ensure_csrf(session)
        return _finish(HTMLResponse(render_login_page(csrf_token=token)), session)

    @app.post("/login")
    async def login_submit(request: Request):
        if not dashboard_password():
            return _unconfigured()
        if not origin_ok(request):
            return _nostore(HTMLResponse("Cross-site request rejected.", status_code=403))
        session = _session(request)
        form = await request.form()
        if not csrf_ok(session, str(form.get("csrf_token") or "")):
            token = ensure_csrf(session)
            page = render_login_page(csrf_token=token, error="Form expired. Try again.")
            return _finish(HTMLResponse(page, status_code=403), session)
        ip = client_ip(request)
        if not limiter.allowed(ip):
            page = render_login_page(
                csrf_token=ensure_csrf(session),
                error="Too many login attempts. Wait and try again.",
            )
            response = _finish(HTMLResponse(page, status_code=429), session)
            response.headers["Retry-After"] = "600"
            return response
        if not password_matches(str(form.get("password") or "")):
            limiter.record_failure(ip)
            token = ensure_csrf(session)
            page = render_login_page(csrf_token=token, error="Wrong password.")
            return _finish(HTMLResponse(page, status_code=401), session)
        limiter.record_success(ip)
        session = {"authenticated": True, "csrf": ensure_csrf({}), "flash": ""}
        return _finish(RedirectResponse("/", status_code=303), session)

    @app.post("/logout")
    async def logout(request: Request):
        if not dashboard_password():
            return _unconfigured()
        session = _session(request)
        form = await request.form()
        if session.get("authenticated") and origin_ok(request) and csrf_ok(
            session, str(form.get("csrf_token") or "")
        ):
            session = {"_drop": True}
        return _finish(RedirectResponse("/login", status_code=303), session)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not dashboard_password():
            return _unconfigured()
        session = _session(request)
        if not session.get("authenticated"):
            return _nostore(RedirectResponse("/login", status_code=303))
        token = ensure_csrf(session)
        events = await loader()
        page = render_dashboard(summarize_events(events), events, csrf_token=token)
        return _finish(HTMLResponse(page), session)

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        if not dashboard_password():
            return _unconfigured()
        session = _session(request)
        if not session.get("authenticated"):
            return _nostore(RedirectResponse("/login", status_code=303))
        flash = str(session.pop("flash", "") or "")
        return await _accounts_response(session, flash=flash)

    @app.post("/accounts")
    async def add_account(request: Request):
        session = _session(request)
        denied, form = await _authorized_form(request, session)
        if denied is not None:
            return denied
        response = None
        store = None
        close_store = False
        try:
            store, close_store = await _use_store()
            await store.add_account(
                str(form.get("name") or ""),
                str(form.get("api_key_id") or ""),
                str(form.get("private_key") or ""),
            )
        except AccountStoreUnavailable as exc:
            response = await _accounts_response(session, unavailable=str(exc), status_code=503)
        except AccountStoreError as exc:
            response = await _accounts_response(
                session, error=str(exc), status_code=400, store=store
            )
        else:
            session["flash"] = "Account saved. The private key will not be shown again."
            response = _finish(RedirectResponse("/accounts", status_code=303), session)
        finally:
            if close_store and store is not None:
                await store.close()
        return response

    async def _mutate(account_id: int, request: Request, action: str):
        session = _session(request)
        denied, form = await _authorized_form(request, session)
        if denied is not None:
            return denied
        response = None
        store = None
        close_store = False
        try:
            store, close_store = await _use_store()
            if action == "delete":
                await store.delete_account(account_id)
                session["flash"] = "Account deleted."
            elif action == "enabled":
                enabled = str(form.get("enabled") or "").lower() == "true"
                await store.set_enabled(account_id, enabled)
                session["flash"] = "Account updated."
            elif action == "live":
                live = str(form.get("live") or "").lower() == "true"
                await store.set_live(account_id, live)
                session["flash"] = (
                    "Dry run."
                    if not live
                    else "Live orders armed for this account when the worker is live."
                )
            else:
                raise AccountStoreError("Unknown action.")
        except AccountStoreUnavailable as exc:
            response = await _accounts_response(session, unavailable=str(exc), status_code=503)
        except AccountStoreError as exc:
            response = await _accounts_response(
                session, error=str(exc), status_code=400, store=store
            )
        else:
            response = _finish(RedirectResponse("/accounts", status_code=303), session)
        finally:
            if close_store and store is not None:
                await store.close()
        return response

    @app.post("/accounts/{account_id}/enabled")
    async def toggle_enabled(account_id: int, request: Request):
        return await _mutate(account_id, request, "enabled")

    @app.post("/accounts/{account_id}/live")
    async def toggle_live(account_id: int, request: Request):
        return await _mutate(account_id, request, "live")

    @app.post("/accounts/{account_id}/delete")
    async def delete_account(account_id: int, request: Request):
        return await _mutate(account_id, request, "delete")

    @app.post("/accounts/{account_id}/test")
    async def test_connection(account_id: int, request: Request):
        session = _session(request)
        denied, _form = await _authorized_form(request, session)
        if denied is not None:
            return denied
        response = None
        store = None
        close_store = False
        message = "Connection failed."
        try:
            store, close_store = await _use_store()
            api_key_id, pem = await store.load_secret(account_id)
            message = await balance_probe(api_key_id, pem)
        except AccountStoreUnavailable as exc:
            response = await _accounts_response(session, unavailable=str(exc), status_code=503)
        except AccountStoreError as exc:
            message = str(exc)
        except Exception:
            logger.info("Kalshi balance check failed for account %s", account_id)
            message = "Connection failed."
        finally:
            if close_store and store is not None:
                await store.close()
        if response is None:
            session["flash"] = message
            response = _finish(RedirectResponse("/accounts", status_code=303), session)
        return response

    @app.api_route("/orders", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def no_orders(request: Request):
        if not dashboard_password():
            return _unconfigured()
        session = _session(request)
        if not session.get("authenticated"):
            return _nostore(JSONResponse(status_code=401, content={"detail": "Login required"}))
        return _nostore(JSONResponse(
            status_code=405,
            content={"detail": "This dashboard cannot place or cancel orders"},
        ))

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
