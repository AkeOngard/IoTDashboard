"""Operator-made groups: list, create, change, delete, and switch.

Every change is pushed to open dashboards over the WebSocket, so a group made
on the phone shows on the wall panel without a reload.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter(prefix="/api/groups", tags=["groups"])


def _hub(request: Request):
    return request.app.state.hub


def _writable(request: Request):
    store = _hub(request).groups
    if not store.enabled:
        raise HTTPException(status_code=503, detail="groups are disabled (DEVICE_GROUPS_PATH is empty)")
    return store


async def _change(request: Request, action) -> Any:
    """Run a store change and turn its failures into HTTP answers."""
    try:
        result = action()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="could not save the group: %s" % exc) from exc
    await _hub(request).groups_changed()
    return result


@router.get("")
async def list_groups(request: Request) -> dict[str, Any]:
    return {"groups": _hub(request).groups.list()}


@router.post("", status_code=201)
async def create_group(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    store = _writable(request)
    return await _change(request, lambda: store.create(body.get("name"), body.get("devices", [])))


@router.patch("/{group_id}")
async def update_group(request: Request, group_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    store = _writable(request)
    if "name" not in body and "devices" not in body:
        raise HTTPException(status_code=422, detail="body needs 'name' and/or 'devices'")
    return await _change(
        request, lambda: store.update(group_id, name=body.get("name"), devices=body.get("devices"))
    )


@router.delete("/{group_id}", status_code=204)
async def delete_group(request: Request, group_id: str) -> None:
    store = _writable(request)
    await _change(request, lambda: store.delete(group_id))


@router.post("/{group_id}/switch")
async def switch_group(request: Request, group_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Turn every switchable member on or off. Automations call the same
    Hub method, so a rule can say "ไฟชั้นล่าง off" without a browser open."""
    value = body.get("value")
    if not isinstance(value, bool):
        raise HTTPException(status_code=422, detail="body needs 'value': true or false")
    try:
        group = _hub(request).groups.get(group_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    sent = _hub(request).switch_many(group["devices"], value)
    return {"group": group_id, "value": value, "devices": sent}
