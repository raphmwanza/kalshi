"""Read-only trade-log server.

Run with ``BOT_MODE=dashboard`` or ``python -m src.journal.web``. It listens
on ``$PORT`` (default 8080) and reads the same journal as the trading
process. There are no order endpoints.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.journal.dashboard import render_dashboard
from src.journal.summary import summarize_events

_basic = HTTPBasic(auto_error=False)


def _password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def _username() -> str:
    return os.environ.get("DASHBOARD_USER", "dashboard") or "dashboard"


async def require_dashboard_auth(
    credentials: HTTPBasicCredentials = Depends(_basic),
) -> str:
    """Reject the page unless DASHBOARD_PASSWORD matches. Missing password fails closed."""
    configured = _password()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Set DASHBOARD_PASSWORD before exposing this dashboard",
        )
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, _username())
    pass_ok = secrets.compare_digest(credentials.password, configured)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dashboard credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def _load_events() -> list:
    from src.journal.tennis_log import open_journal

    journal = await open_journal()
    try:
        return await journal.load_events()
    finally:
        await journal.close()


def create_app(load_events=None) -> FastAPI:
    """Build the read-only app. Tests pass ``load_events`` so no database is required."""
    loader = load_events or _load_events
    app = FastAPI(title="Tennis journal", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index(_user: str = Depends(require_dashboard_auth)):
        events = await loader()
        summary = summarize_events(events)
        return HTMLResponse(render_dashboard(summary, events))

    @app.api_route("/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def no_writes(path: str, request: Request):
        return JSONResponse(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            content={"detail": "This dashboard is read-only"},
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
