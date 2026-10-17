"""The 3D house page's plan and device places: read, save, reset.

Every change goes out to open dashboards as a bare {"type": "house"}: the
plan's default wording depends on each viewer's language, so a dashboard on
the house page fetches its own copy rather than being sent one.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from app.i18n import lang_of

router = APIRouter(prefix="/api/house", tags=["house"])


def _store(request: Request):
    return request.app.state.house


def _writable(request: Request):
    store = _store(request)
    if not store.enabled:
        raise HTTPException(status_code=503, detail="the house plan is read-only (HOUSE_PATH is empty)")
    return store


async def _change(request: Request, action) -> Any:
    try:
        result = action()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="could not save the house plan: %s" % exc) from exc
    await request.app.state.hub.publish({"type": "house"})
    return result


@router.get("")
async def get_house(request: Request) -> dict[str, Any]:
    return _store(request).view(lang_of(request))


@router.put("/layout", status_code=204)
async def save_layout(request: Request, body: dict[str, Any] = Body(...)) -> None:
    store = _writable(request)
    await _change(request, lambda: store.set_layout(body.get("layout")))


@router.delete("/layout", status_code=204)
async def reset_layout(request: Request) -> None:
    store = _writable(request)
    await _change(request, store.reset_layout)


@router.put("/placements/{device_id}")
async def place_device(request: Request, device_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    store = _writable(request)
    return await _change(request, lambda: store.place(device_id, body))


@router.delete("/placements/{device_id}", status_code=204)
async def unplace_device(request: Request, device_id: str) -> None:
    store = _writable(request)
    await _change(request, lambda: store.unplace(device_id))


@router.put("/lamps/{key}", status_code=204)
async def bind_lamp(request: Request, key: str, body: dict[str, Any] = Body(...)) -> None:
    """Tie a lamp ("<floor>:<lamp>") to the device that switches it, or to
    nothing ({"device": null}) so it never lights up."""
    store = _writable(request)
    if "device" not in body:
        raise HTTPException(status_code=422, detail="body needs 'device': a device id or null")
    await _change(request, lambda: store.bind_lamp(key, body["device"]))


@router.delete("/lamps/{key}", status_code=204)
async def unbind_lamp(request: Request, key: str) -> None:
    """Let the browser guess the lamp's device from its room again."""
    store = _writable(request)
    await _change(request, lambda: store.unbind_lamp(key))
