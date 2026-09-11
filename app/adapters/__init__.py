"""Adapter selection (doc §5: Matter primary, Tuya fallback, router on top)."""
from __future__ import annotations

from app.adapters.base import DeviceAdapter


def build_adapter(kind: str) -> DeviceAdapter:
    kind = (kind or "mock").lower()

    if kind == "matter":
        from app.adapters.matter_adapter import MatterAdapter

        return MatterAdapter()

    if kind == "tuya":
        from app.adapters.tuya_adapter import TuyaAdapter

        return TuyaAdapter()

    if kind == "hybrid":
        from app.adapters.matter_adapter import MatterAdapter
        from app.adapters.router import DeviceRouter
        from app.adapters.tuya_adapter import TuyaAdapter

        # Order matters: the first adapter is the primary one.
        return DeviceRouter([MatterAdapter(), TuyaAdapter()])

    if kind == "mock":
        from app.adapters.mock_adapter import MockAdapter

        return MockAdapter()

    raise ValueError(
        "unknown IOT_ADAPTER %r (expected matter, tuya, hybrid or mock)" % kind
    )
