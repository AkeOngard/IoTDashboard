"""Settings, per the documented ENV contract (Quick Reference §17)."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
MIGRATIONS_DIR = ROOT / "migrations"


def _secrets_dirs() -> list[Path]:
    """Where a setting may live as a file of its own instead of in .env.

    A file named after the setting -- `toshiba_password`, `database_url` --
    holds its value and nothing else. Inside the container that is the
    read-only /run/secrets mount; running uvicorn on the host, ./secrets.
    Only directories that exist are passed on: pydantic warns about the rest.

    Why bother: an environment variable is on show to anyone who can run
    `docker inspect` or `docker compose config`, and in /proc/<pid>/environ.
    A file readable only by the app's user is not.
    """
    candidates = [Path(os.environ.get("SECRETS_DIR", "/run/secrets")), ROOT / "secrets"]
    # One we may not read is skipped, not fatal: scripts/set_secret.sh hands
    # the directory to the container's user, so `make migrate` on the host
    # sees it but cannot open it -- and must still start.
    return [p for p in dict.fromkeys(candidates) if _readable_dir(p)]


def _readable_dir(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.R_OK | os.X_OK)
    except OSError:     # a parent we may not enter
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
        secrets_dir=_secrets_dirs() or None,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # A secret file wins over the environment, not the other way round as
        # pydantic has it: compose passes `TOSHIBA_PASSWORD: ${TOSHIBA_PASSWORD:-}`,
        # an empty string, whenever .env leaves it out -- and that empty
        # string would otherwise mask the file that was put there on purpose.
        return init_settings, file_secret_settings, env_settings, dotenv_settings

    # --- device layer -------------------------------------------------------
    #: "matter" | "tuya" | "toshiba" | "mock", or several joined by commas
    #: ("matter,toshiba"), the first being primary. "hybrid" = "matter,tuya".
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

    #: Toshiba Home AC Control -- also what Carrier's "Carrier In The Air"
    #: units in Thailand run on. The same login as the phone app. Only needed
    #: when iot_adapter includes "toshiba".
    toshiba_username: str = ""
    toshiba_password: str = ""
    #: Where this dashboard keeps the id it registered with Toshiba's cloud
    #: under. Every client of one account needs its own: two sharing an id
    #: knock each other off the connection in turn.
    toshiba_state_path: str = str(ROOT / "data" / "toshiba.json")

    # --- storage ------------------------------------------------------------
    #: Empty disables persistence; the app then runs live-only.
    database_url: str = ""
    #: Migrations are normally applied by the `migrate` compose service, not by
    #: the app. The app only verifies on boot (doc §12 startup order).
    run_migrations: bool = False
    #: Local device names, kept outside the database so they survive the
    #: Pi-only deployment (DATABASE_URL empty). Set empty to disable renaming.
    device_labels_path: str = str(ROOT / "data" / "labels.json")
    #: Groups the operator makes ("ไฟชั้นล่าง"), beside the labels for the
    #: same reason. Set empty to disable making groups; rooms still work.
    device_groups_path: str = str(ROOT / "data" / "groups.json")
    #: Automation rules. Set empty to switch automations off entirely.
    automations_path: str = str(ROOT / "data" / "automations.json")
    #: The house's clock, for time rules. The container itself runs on UTC,
    #: so "22:30" would otherwise mean 05:30 in Bangkok.
    timezone: str = "Asia/Bangkok"
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
    # --- login --------------------------------------------------------------
    #: Where the password hash and session secret live. A file, not the
    #: database: losing history must never lock the operator out of their own
    #: lights. Empty disables login entirely -- only sane behind a network you
    #: already trust, and the startup log says so.
    auth_path: str = str(ROOT / "data" / "auth.json")
    #: Optional. Left empty, a secret is generated once and kept in auth_path,
    #: so sessions survive a restart with nothing to configure.
    session_secret: str = ""
    #: How long a browser stays signed in. A month, because a dashboard opened
    #: from the sofa that asks daily teaches the operator to pick a short
    #: password.
    session_hours: float = 24 * 30
    #: Failed logins allowed per client address per window. Per address on
    #: purpose: a global lockout would let anyone who can reach the login page
    #: lock the owner out of the house.
    login_attempts: int = 10
    login_window_minutes: float = 15.0

    #: Commissioning hands out fabric membership. Behind any proxy or port
    #: mapping every caller looks like a private address, so a "LAN only"
    #: check proves nothing -- the HTTP route stays off unless asked for, and
    #: `scripts/commission.py` (needs a shell on the host) is the normal path.
    allow_http_commission: bool = False

    @property
    def trusted_hosts(self) -> list[str]:
        hosts = _split(self.allowed_hosts) or ["*"]
        if "*" in hosts:
            if not self.auth_path:
                # With no login, the Host check is all that stops a page the
                # operator visits from reaching this port through DNS
                # rebinding. "Any host" and "no password" together would leave
                # the house open to every website, so fall back to loopback --
                # list real names in ALLOWED_HOSTS to be reachable elsewhere.
                return ["127.0.0.1", "localhost"]
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
