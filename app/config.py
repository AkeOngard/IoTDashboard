"""Settings, per the documented ENV contract (Quick Reference §17)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
MIGRATIONS_DIR = ROOT / "migrations"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- device layer -------------------------------------------------------
    #: "matter" | "tuya" | "hybrid" | "mock"
    iot_adapter: str = "mock"
    matter_ws_url: str = "ws://127.0.0.1:5580/ws"
    command_timeout: float = 5.0
    #: How long to wait for a matter-server reply. The CHIP stack can block
    #: its event loop for seconds on a slow board, so keep this generous.
    matter_rpc_timeout: float = 30.0

    #: Tuya Cloud, only needed when iot_adapter is "tuya" or "hybrid".
    tuya_access_id: str = ""
    tuya_access_secret: str = ""
    tuya_api_region: str = "us"
    tuya_uid: str = ""
    #: Doc §14: never poll Tuya faster than 10s per device.
    tuya_poll_seconds: float = 15.0

    # --- storage ------------------------------------------------------------
    #: Empty disables persistence; the app then runs live-only.
    database_url: str = ""
    #: Migrations are normally applied by the `migrate` compose service, not by
    #: the app. The app only verifies on boot (doc §12 startup order).
    run_migrations: bool = False
    #: Local device names, kept outside the database so they survive the
    #: Pi-only deployment (DATABASE_URL empty). Set empty to disable renaming.
    device_labels_path: str = str(ROOT / "data" / "labels.json")
    #: How long telemetry sits in memory before a batched INSERT. Worth raising
    #: over a slow or metered link to a managed database: it trades a little
    #: freshness in the chart for fewer round trips.
    telemetry_flush_seconds: float = 2.0
    #: Retention for plain PostgreSQL, where there is no TimescaleDB policy to
    #: do it. Ignored on TimescaleDB (migration 005 owns retention there).
    #: 0 disables pruning entirely -- the table then grows without limit.
    history_retention_days: float = 400.0

    # --- http ---------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    public_origin: str = "http://localhost:8000"
    #: Comma-separated in the environment; kept as a string so pydantic-settings
    #: does not try to JSON-decode it. Use `trusted_hosts` / `cors_origins`.
    allowed_hosts: str = "*"
    #: Doc §6: Cloudflare drops WebSockets idle for ~100s.
    ws_ping_seconds: float = 25.0
    #: Commissioning hands out fabric membership. Behind any proxy or port
    #: mapping every caller looks like a private address, so a "LAN only"
    #: check proves nothing -- the HTTP route stays off unless asked for, and
    #: `scripts/commission.py` (needs a shell on the host) is the normal path.
    allow_http_commission: bool = False

    @property
    def trusted_hosts(self) -> list[str]:
        hosts = _split(self.allowed_hosts) or ["*"]
        if "*" in hosts:
            return ["*"]
        # The container HEALTHCHECK and `make health` call 127.0.0.1; forgetting
        # it in ALLOWED_HOSTS would mark a healthy container unhealthy.
        return hosts + [h for h in ("127.0.0.1", "localhost") if h not in hosts]

    @property
    def cors_origins(self) -> list[str]:
        return _split(self.public_origin)


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


settings = Settings()
