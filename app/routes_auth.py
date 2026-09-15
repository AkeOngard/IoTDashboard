"""Login, logout, and changing the password."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response

from app.auth import SESSION_COOKIE, AuthError, password_problem
from app.config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _store(request: Request):
    store = request.app.state.auth
    # /api/auth/* is public, so the gate has not looked at the file for us.
    store.reload_if_changed()
    return store


def _throttle(request: Request):
    return request.app.state.login_throttle


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _secure(request: Request) -> bool:
    """Only mark the cookie Secure when the browser really is on https, or the
    cookie would be dropped on a plain-http LAN visit and login would appear to
    do nothing."""
    return request.url.scheme == "https" or settings.public_origin.startswith("https://")


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

    key = _client(request)
    wait = throttle.blocked_for(key)
    if wait > 0:
        # 429 with Retry-After, so a script backs off and a human is told how
        # long rather than being left to guess.
        raise HTTPException(
            status_code=429,
            detail="ใส่รหัสผิดหลายครั้งเกินไป ลองใหม่ในอีก %d นาที" % max(1, round(wait / 60)),
            headers={"Retry-After": str(int(wait))},
        )

    password = body.get("password")
    ok = False
    if isinstance(password, str) and password:
        try:
            ok = store.check_password(password)
        except AuthError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not ok:
        throttle.record_failure(key)
        log.warning("failed login from %s", key)
        raise HTTPException(status_code=401, detail="wrong password")

    throttle.clear(key)
    ttl = settings.session_hours * 3600
    token, _ = store.issue(ttl)
    set_session_cookie(request, response, token, int(ttl))
    log.info("login from %s", key)
    return {"status": "ok"}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.post("/password")
async def change_password(
    request: Request, response: Response, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    store = _store(request)
    current = body.get("current")
    new = body.get("new")
    if not isinstance(new, str):
        raise HTTPException(status_code=422, detail="body needs 'new'")

    # Changing the password is how someone who thinks they were compromised
    # locks everyone else out, so prove the current one even though this
    # request already carries a valid session.
    if not isinstance(current, str) or not store.check_password(current):
        raise HTTPException(status_code=401, detail="current password is wrong")

    problem = password_problem(new)
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    store.set_password(new)
    # set_password bumps the generation, which voided the cookie this request
    # arrived with; hand back a fresh one so the operator is not signed out of
    # the tab they just used.
    ttl = settings.session_hours * 3600
    token, _ = store.issue(ttl)
    set_session_cookie(request, response, token, int(ttl))
    log.info("password changed from %s; all other sessions signed out", _client(request))
    return {"status": "ok"}
