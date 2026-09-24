"""Login, logout, and changing the password."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response

from app.auth import (
    DEVICE_COOKIE,
    DEVICE_TTL_SECONDS,
    SESSION_COOKIE,
    AuthError,
    hash_password,
    password_problem,
)
from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: scrypt runs off the event loop -- ~200 ms of CPU on a Pi would otherwise
#: freeze every dashboard and WebSocket for each guess -- and at most two at
#: once, because each one also takes 16 MB and the Pi container has 256.
_HASH_SLOTS = asyncio.Semaphore(2)


async def _off_loop(fn, *args):
    async with _HASH_SLOTS:
        return await asyncio.to_thread(fn, *args)


def _store(request: Request):
    store = request.app.state.auth
    # /api/auth/* is public, so the gate has not looked at the file for us.
    store.reload_if_changed()
    return store


def _throttle(request: Request):
    return request.app.state.login_throttle


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttle_key(request: Request, store) -> str:
    """Whose failed attempts these are.

    A browser that has signed in before carries a device cookie and is counted
    on its own. Everyone else is counted by address -- and behind a proxy or a
    Docker port mapping every stranger may share one address, which is why
    the owner's browser must not be counted with them: ten bad guesses from
    anywhere would otherwise lock the owner out of their own house.
    """
    device = store.device_id(request.cookies.get(DEVICE_COOKIE))
    return "device:" + device if device else "ip:" + _client(request)


def _check_throttle(request: Request, key: str) -> None:
    wait = _throttle(request).blocked_for(key)
    if wait > 0:
        # 429 with Retry-After, so a script backs off and a human is told how
        # long rather than being left to guess.
        raise HTTPException(
            status_code=429,
            detail="ใส่รหัสผิดหลายครั้งเกินไป ลองใหม่ในอีก %d นาที" % max(1, round(wait / 60)),
            headers={"Retry-After": str(int(wait))},
        )


def _secure(request: Request) -> bool:
    """Mark the cookie Secure only when *this* request arrived over https.

    Not when PUBLIC_ORIGIN merely says https. The same dashboard is reached
    both ways -- https through `tailscale serve`, plain http on the LAN -- and
    a browser silently discards a Secure cookie set on an http page. Login then
    returns 200, the redirect finds no session, and the operator is bounced back
    to the login page with no error at all. That is exactly what happened on the
    Pi, whose .env sets an https PUBLIC_ORIGIN.

    Behind the tailscale proxy the scheme still reads https, because uvicorn
    trusts X-Forwarded-Proto from 127.0.0.1 (see scripts/entrypoint.sh).
    """
    return request.url.scheme == "https"


def set_session_cookie(request: Request, response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        # Strict, not Lax: nothing links into this dashboard from elsewhere, so
        # there is no navigation to preserve, and Strict keeps the session out
        # of any request another site can cause.
        samesite="strict",
        secure=_secure(request),
        path="/",
    )


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    store = _store(request)
    return {
        "required": store.enabled,
        "configured": store.configured,
        "authenticated": store.valid(request.cookies.get(SESSION_COOKIE)),
    }


@router.post("/login")
async def login(
    request: Request, response: Response, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    store = _store(request)
    throttle = _throttle(request)
    if not store.configured:
        raise HTTPException(
            status_code=503,
            detail="no password has been set; run scripts/set_password.py on the host",
        )

    key = _throttle_key(request, store)
    _check_throttle(request, key)

    password = body.get("password")
    ok = False
    if isinstance(password, str) and password:
        try:
            ok = await _off_loop(store.check_password, password)
        except AuthError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        throttle.record_failure(key)
        log.warning("failed login from %s (%s)", _client(request), key.split(":")[0])
        raise HTTPException(status_code=401, detail="wrong password")

    throttle.clear(key)
    ttl = settings.session_hours * 3600
    token, _ = store.issue(ttl)
    set_session_cookie(request, response, token, int(ttl))
    _remember_device(request, response, store)
    log.info("login from %s", _client(request))
    return {"status": "ok"}


def _remember_device(request: Request, response: Response, store) -> None:
    """Hand out a device cookie, unless this browser already holds a good one."""
    if store.device_id(request.cookies.get(DEVICE_COOKIE)):
        return
    response.set_cookie(
        DEVICE_COOKIE,
        store.issue_device(),
        max_age=DEVICE_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=_secure(request),
        path="/api/auth/",
    )


def _clear_cookie(request: Request, response: Response) -> None:
    # Same attributes as when it was set, so every browser treats this as the
    # same cookie and drops it.
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=_secure(request)
    )


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    """End this session on the server, then tell the browser to forget it.

    Clearing the cookie alone is a request to the browser; a copy of the token
    taken earlier would keep working for the rest of its 30 days. Revoking it
    here is what makes logging out mean something.
    """
    store = _store(request)
    ended = store.revoke(request.cookies.get(SESSION_COOKIE))
    _clear_cookie(request, response)
    if ended:
        log.info("logout from %s", _client(request))
    return {"status": "ok"}


@router.post("/logout-all")
async def logout_all(request: Request, response: Response) -> dict[str, Any]:
    """Sign every browser out, this one included, keeping the password."""
    store = _store(request)
    # Only someone already signed in may do this: /api/auth/* skips the gate,
    # and ending the owner's sessions is not something to hand to a stranger.
    if not store.valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="not signed in")
    store.revoke_all()
    _clear_cookie(request, response)
    log.info("all sessions ended from %s", _client(request))
    return {"status": "ok"}


@router.post("/password")
async def change_password(
    request: Request, response: Response, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    store = _store(request)
    # /api/auth/* skips the gate, so this route has to check for itself.
    # Without it, anyone could use the "current password" check below as a
    # password oracle that the login throttle never sees -- and a right guess
    # would hand them the house and lock the owner out.
    if not store.valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="not signed in")

    current = body.get("current")
    new = body.get("new")
    if not isinstance(current, str) or not isinstance(new, str):
        raise HTTPException(status_code=422, detail="body needs 'current' and 'new'")
    problem = password_problem(new)
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    # Changing the password is how someone who thinks they were compromised
    # locks everyone else out, so prove the current one even though this
    # request carries a valid session -- a stolen cookie must not be enough.
    # Throttled like login: it is a password check all the same.
    key = _throttle_key(request, store)
    _check_throttle(request, key)
    try:
        ok = await _off_loop(store.check_password, current)
    except AuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        _throttle(request).record_failure(key)
        log.warning("wrong current password on change from %s", _client(request))
        raise HTTPException(status_code=401, detail="current password is wrong")
    _throttle(request).clear(key)

    store.set_password_hash(await _off_loop(hash_password, new))
    # set_password_hash bumps the generation, which voided the cookie this
    # request arrived with; hand back a fresh one so the operator is not signed
    # out of the tab they just used.
    ttl = settings.session_hours * 3600
    token, _ = store.issue(ttl)
    set_session_cookie(request, response, token, int(ttl))
    _remember_device(request, response, store)
    log.info("password changed from %s; all other sessions signed out", _client(request))
    return {"status": "ok"}
