"""Adapter selection (doc §5: Matter primary, cloud fallbacks, router on top)."""
from __future__ import annotations

from app.adapters.base import DeviceAdapter

#: Shorthands for a list of adapters.
ALIASES = {"hybrid": "matter,tuya"}
KNOWN = ("matter", "tuya", "toshiba", "mock")


def build_adapter(kind: str) -> DeviceAdapter:
    """One adapter, or a DeviceRouter over several: "matter,toshiba".

    Imports are deferred so a backend that is not asked for costs nothing --
    the Toshiba one pulls in the Azure IoT SDK and its threads.
    """
    spec = (kind or "mock").lower().strip()
    names = [n.strip() for n in ALIASES.get(spec, spec).split(",") if n.strip()]
    unknown = [n for n in names if n not in KNOWN]
    if not names or unknown:
        raise ValueError(
            "unknown IOT_ADAPTER %r (expected %s, hybrid, or several joined by commas)"
            % (kind, ", ".join(KNOWN))
        )
    if len(set(names)) != len(names):
        raise ValueError("IOT_ADAPTER %r names an adapter twice" % kind)

    adapters = [_one(n) for n in names]
    if len(adapters) == 1:
        return adapters[0]

    from app.adapters.router import DeviceRouter

    # Order matters: the first adapter is the primary one.
    return DeviceRouter(adapters)


def _one(name: str) -> DeviceAdapter:
    if name == "matter":
        from app.adapters.matter_adapter import MatterAdapter

        return MatterAdapter()
    if name == "tuya":
        from app.adapters.tuya_adapter import TuyaAdapter

        return TuyaAdapter()
    if name == "toshiba":
        from app.adapters.toshiba_adapter import ToshibaAdapter

        return ToshibaAdapter()
    from app.adapters.mock_adapter import MockAdapter

    return MockAdapter()
