"""Device routes (doc §6). Auth dependencies land here in the security phase."""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app import history
from app.config import settings
from app.models import CommandError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["devices"])


def _hub(request: Request):
    return request.app.state.hub


def _db(request: Request):
    return request.app.state.db


async def _command(request: Request, device_id: str, capability: str, value: Any) -> dict[str, Any]:
    try:
        return await _hub(request).execute(device_id, capability, value)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("")
async def list_devices(request: Request) -> dict[str, Any]:
    snapshot = _hub(request).snapshot()
    snapshot["history"] = _db(request).available
    return snapshot


@router.post("/refresh")
async def refresh_devices(request: Request) -> dict[str, Any]:
    hub = _hub(request)
    await hub.refresh()
    return hub.snapshot()


@router.post("/{device_id}/onoff")
async def set_onoff(request: Request, device_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if "value" not in body:
        raise HTTPException(status_code=422, detail="body needs 'value'")
    return await _command(request, device_id, "switch", body["value"])


@router.post("/{device_id}/level")
async def set_level(request: Request, device_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if "value" not in body:
        raise HTTPException(status_code=422, detail="body needs 'value'")
    return await _command(request, device_id, body.get("capability", "brightness"), body["value"])


@router.post("/{device_id}/command")
async def send_command(request: Request, device_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Generic form of the two above -- what the dashboard actually calls."""
    capability = body.get("capability")
    if not capability or "value" not in body:
        raise HTTPException(status_code=422, detail="body needs 'capability' and 'value'")
    return await _command(request, device_id, str(capability), body["value"])


@router.get("/{device_id}/history")
async def device_history(
    request: Request,
    device_id: str,
    capability: str = Query(...),
    hours: float = Query(24.0, gt=0, le=24 * 365),
    points: int = Query(240, ge=10, le=1000),
) -> dict[str, Any]:
    db = _db(request)
    if not db.enabled:
        raise HTTPException(status_code=503, detail="history is disabled (DATABASE_URL is empty)")
    if not db.available:
        raise HTTPException(status_code=503, detail="database unavailable: " + (db.last_error or ""))
    try:
        return await history.series(db, device_id, capability, hours, points)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="unknown capability " + capability) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/commission")
async def commission(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Join a Matter device to our fabric with an 11-digit pairing code.

    Doc §14: LAN only. Commissioning hands out fabric membership, so it must
    never be reachable through a tunnel. The private-address check alone cannot
    guarantee that -- behind a port mapping or reverse proxy every caller has a
    private address -- so the route is off unless ALLOW_HTTP_COMMISSION is set.
    """
    if not settings.allow_http_commission:
        raise HTTPException(
            status_code=403,
            detail="HTTP commissioning is disabled; run scripts/commission.py on the host "
            "(or set ALLOW_HTTP_COMMISSION=1)",
        )
    if not _is_lan(request):
        raise HTTPException(status_code=403, detail="commissioning is allowed from the LAN only")
    code = str(body.get("code", "")).strip()
    if not code:
        raise HTTPException(status_code=422, detail="body needs 'code'")

    adapter = getattr(_hub(request), "adapter", None)
    commission_fn = getattr(adapter, "commission", None)
    if commission_fn is None:
        raise HTTPException(status_code=400, detail="the active adapter cannot commission devices")
    try:
        node = await commission_fn(code)
    except CommandError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await _hub(request).refresh()
    return {"status": "commissioned", "node": node}


def _is_lan(request: Request) -> bool:
    client = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return False
    return address.is_private or address.is_loopback
