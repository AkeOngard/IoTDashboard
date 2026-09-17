"""FastAPI application (doc §6): middleware stack, routes, WebSocket hub."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.adapters import build_adapter
from app.auth import SESSION_COOKIE, AuthStore, LoginThrottle
from app.config import STATIC_DIR, TEMPLATES_DIR, settings
from app.db import Database
from app.hub import Hub
from app.labels import LabelStore
from app.routes_auth import router as auth_router
from app.routes_devices import router as devices_router
from app.telemetry import Recorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("iot")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_static_versions: dict[str, tuple[float, str]] = {}


def static_url(name: str) -> str:
    """/static/<name>?v=<content hash>, so a deploy reaches browsers at once.

    Without it a browser keeps an old script for as long as its own guess
    says -- days, for a file that had not changed in weeks -- and runs it
    against new HTML: the new logout button called a logout() the cached
    dashboard.js did not have yet, and the click did nothing at all.
    """
    path = STATIC_DIR / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "/static/" + name
    cached = _static_versions.get(name)
    if cached is None or cached[0] != mtime:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        cached = _static_versions[name] = (mtime, digest)
    return "/static/%s?v=%s" % (name, cached[1])


templates.env.globals["static_url"] = static_url

# Alpine.js evaluates expressions with the Function constructor, so 'unsafe-eval'
# is required until we switch to its CSP build; Tailwind's play CDN injects a
# <style> element, hence 'unsafe-inline' for styles.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        # Doc §14: never let a service worker or proxy cache the API.
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        # Always ask before reusing a static file. The ETag makes that a 304
        # of a few hundred bytes; skipping the question is how a browser ended
        # up running last week's dashboard.js (see static_url).
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response


#: Reachable without a session. Everything not on this list needs one.
#: /healthz and /readyz stay open because the container HEALTHCHECK and any
#: uptime monitor cannot log in -- they answer on loopback and say nothing a
#: stranger could act on.
PUBLIC_PATHS = frozenset({"/login", "/healthz", "/readyz", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/", "/api/auth/")


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class RequireLoginMiddleware(BaseHTTPMiddleware):
    """One gate in front of everything, rather than a decorator per route.

    A route added later is protected by default; forgetting to guard a new
    endpoint is the usual way a dashboard like this springs a leak.
    """

    async def dispatch(self, request: Request, call_next):
        store: AuthStore = request.app.state.auth
        path = request.url.path
        if not store.enabled or _is_public(path):
            return await call_next(request)
        store.reload_if_changed()

        # No password set yet: refuse to serve anything rather than run open.
        if not store.configured:
            if path == "/" or "text/html" in request.headers.get("accept", ""):
                return templates.TemplateResponse(
                    request, "login.html", {"setup_needed": True}, status_code=503
                )
            return JSONResponse(
                {"detail": "no password has been set; run scripts/set_password.py"},
                status_code=503,
            )

        if store.valid(request.cookies.get(SESSION_COOKIE)):
            return await call_next(request)

        # A browser asking for a page gets the login page; anything else gets
        # a 401 that static/api.js already knows how to act on.
        if "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(request, "login.html", status_code=401)
        return JSONResponse({"detail": "not signed in"}, status_code=401)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.database_url)
    await db.start()
    app.state.db = db
    app.state.migrations = await _migrations(db)
    # Migrations can create the continuous aggregate, which the probe during
    # db.start() ran too early to see. Ask again now that the schema is final.
    await db.probe()

    recorder = Recorder(
        db,
        flush_interval=settings.telemetry_flush_seconds,
        retention_days=settings.history_retention_days,
    )
    await recorder.start()
    app.state.recorder = recorder

    labels = LabelStore(settings.device_labels_path or None)
    labels.load()
    app.state.labels = labels

    auth = AuthStore(settings.auth_path or None, settings.session_secret)
    auth.load()
    app.state.auth = auth
    app.state.login_throttle = LoginThrottle(
        limit=settings.login_attempts, window=settings.login_window_minutes * 60
    )

    adapter = build_adapter(settings.iot_adapter)
    hub = Hub(
        adapter,
        command_timeout=settings.command_timeout,
        recorder=recorder,
        labels=labels,
    )
    app.state.hub = hub

    log.info("starting with %s adapter (history: %s)", adapter.name, "on" if db.enabled else "off")
    hub.seed_states(await recorder.load_last_states())
    await hub.start()
    try:
        yield
    finally:
        await hub.stop()
        await recorder.stop()
        await db.stop()


async def _migrations(db: Database) -> dict[str, Any]:
    """Migrations are normally applied by the `migrate` compose service before
    the app starts (doc §12). The app verifies, and only applies when explicitly
    told to -- useful when running uvicorn directly in development."""
    if not db.available:
        return {"checked": False}
    from scripts import migrate as migrate_tool

    try:
        async with db.pool.acquire() as conn:  # type: ignore[union-attr]
            if settings.run_migrations:
                result = await migrate_tool.up(conn)
                if result["applied"]:
                    log.info("applied migrations: %s", ", ".join(result["applied"]))
                if result["skipped"]:
                    log.info("skipped migrations: %s", ", ".join(result["skipped"]))
            state = await migrate_tool.status(conn)
        if state["drift"]:
            log.error("migration checksum drift: %s", ", ".join(state["drift"]))
        elif state["pending"]:
            log.warning("pending migrations: %s -- run `make migrate`", ", ".join(state["pending"]))
        return {"checked": True, **state}
    except Exception as exc:  # noqa: BLE001 - schema state must not block control
        log.error("migration check failed: %s", exc)
        return {"checked": False, "error": str(exc)}


app = FastAPI(title="IoT Control Gateway", version="0.3.0", lifespan=lifespan)

app.add_middleware(RequireLoginMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

app.include_router(auth_router)
app.include_router(devices_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------- health


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    hub: Hub = app.state.hub
    db: Database = app.state.db
    recorder: Recorder = app.state.recorder
    snapshot = hub.snapshot()

    database: dict[str, Any] = {"enabled": db.enabled, "connected": db.available}
    if db.enabled and not db.available:
        database["error"] = db.last_error
    database["migrations"] = app.state.migrations
    if db.available:
        # Which history path is live. A chart that looks coarser than expected
        # is usually this line saying "plain postgresql, no rollup".
        database["features"] = db.features

    backends = getattr(hub.adapter, "backends", None)
    degraded = not snapshot["connected"] or (db.enabled and not db.available)

    # Public so the container HEALTHCHECK and an uptime monitor can reach it,
    # so an unauthenticated caller gets liveness and nothing else: how many
    # devices are in the house is not their business.
    store: AuthStore = app.state.auth
    if store.enabled and not store.valid(request.cookies.get(SESSION_COOKIE)):
        return {"status": "degraded" if degraded else "ok"}

    return {
        "status": "degraded" if degraded else "ok",
        "adapter": snapshot["adapter"],
        "adapter_connected": snapshot["connected"],
        **({"backends": backends} if backends else {}),
        "devices": len(snapshot["devices"]),
        "database": database,
        "telemetry": recorder.stats,
    }


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: can this instance actually serve its purpose right now?"""
    hub: Hub = app.state.hub
    db: Database = app.state.db
    ready = hub.snapshot()["connected"] and (not db.enabled or db.available)
    return JSONResponse({"ready": ready}, status_code=200 if ready else 503)


# ------------------------------------------------------------------ websocket


@app.websocket("/ws")
async def stream(websocket: WebSocket) -> None:
    # BaseHTTPMiddleware only runs for http scopes, so the gate above never
    # sees this connection: check here or the socket is wide open.
    store: AuthStore = app.state.auth
    store.reload_if_changed()
    if store.enabled and not store.valid(websocket.cookies.get(SESSION_COOKIE)):
        # Accept, then close with 1008. Closing *before* accepting rejects the
        # handshake with an HTTP 403, and a browser reports that as a plain
        # 1006 "abnormal closure" -- indistinguishable from the server having
        # gone away, which the client answers by reconnecting forever. Taking
        # the socket and closing it politely is what makes the reason legible.
        await websocket.accept()
        await websocket.close(code=1008, reason="not signed in")
        return
    await websocket.accept()
    hub: Hub = app.state.hub
    queue = hub.subscribe()
    reader = asyncio.create_task(_drain(websocket))
    try:
        snapshot = hub.snapshot()
        snapshot["history"] = app.state.db.available
        await websocket.send_json(snapshot)
        while not reader.done():
            try:
                message = await asyncio.wait_for(queue.get(), timeout=settings.ws_ping_seconds)
            except asyncio.TimeoutError:
                # Doc §6: Cloudflare drops a WebSocket idle for ~100s, and a
                # quiet house is a normal state, so heartbeat through the gap.
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(message)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except RuntimeError:
        # Socket closed underneath us mid-send.
        pass
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        hub.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _drain(websocket: WebSocket) -> None:
    """Consume client frames ("pong") and, more importantly, notice hangups."""
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await websocket.receive_text()


# ----------------------------------------------------------------------- page


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/login")
async def login_page(request: Request):
    store: AuthStore = app.state.auth
    if not store.enabled or store.valid(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"setup_needed": not store.configured}
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)
