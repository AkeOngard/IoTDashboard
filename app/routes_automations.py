"""Automation rules: list, create, change (including on/off), delete, test.

Every change is pushed to open dashboards as an "automations" message; every
run as an "automation_run" message.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter(prefix="/api/automations", tags=["automations"])


def _engine(request: Request):
    return request.app.state.automations


def _writable(request: Request):
    engine = _engine(request)
    if not engine.store.enabled:
        raise HTTPException(status_code=503, detail="automations are disabled (AUTOMATIONS_PATH is empty)")
    return engine


def _guard(action) -> Any:
    try:
        return action()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="could not save the rule: %s" % exc) from exc


@router.get("")
async def list_rules(request: Request) -> dict[str, Any]:
    return _engine(request).snapshot()


@router.post("", status_code=201)
async def create_rule(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    engine = _writable(request)
    rule = _guard(lambda: engine.store.create(body))
    await engine.rules_changed(changed=rule["id"])
    return rule


@router.patch("/{rule_id}")
async def update_rule(request: Request, rule_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """A whole rule, or just part of one -- {"enabled": false} pauses it."""
    engine = _writable(request)
    rule = _guard(lambda: engine.store.update(rule_id, body))
    await engine.rules_changed(changed=rule_id)
    return rule


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(request: Request, rule_id: str) -> None:
    engine = _writable(request)
    _guard(lambda: engine.store.delete(rule_id))
    await engine.rules_changed(removed=rule_id)


@router.post("/{rule_id}/run")
async def test_rule(request: Request, rule_id: str) -> dict[str, Any]:
    """Run a rule's actions now, whatever its trigger and conditions say --
    to see that it does what it should."""
    _guard(lambda: _engine(request).test(rule_id))
    return {"rule": rule_id, "started": True}
