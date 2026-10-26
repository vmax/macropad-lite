"""Host configuration: one TOML file, plain dataclasses, explicit defaults.

Default location: ~/.config/macropadd/config.toml (override with
MACROPADD_CONFIG). A missing file yields the defaults; a malformed one is
an error, never a silent fallback.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path("~/.config/macropadd/config.toml")

IS_MAC = sys.platform == "darwin"
DEFAULT_GLOB = "/dev/cu.usbmodem*" if IS_MAC else "/dev/ttyACM*"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    serial_glob: str = DEFAULT_GLOB
    serial_port: str | None = None      # explicit data port; None = auto-detect
    serial_baud: int = 115200
    reconnect_s: float = 1.0
    ping_interval_s: float = 2.0
    log_level: str = "INFO"
    agents_approve: bool = True          # answer permission prompts from the pad
    agents_approve_wait_s: float = 30.0  # then the agent asks in its terminal
    agents_keep_awake: bool = True       # no system sleep while agents work


def from_dict(data: dict) -> Config:
    serial = data.get("serial", {})
    daemon = data.get("daemon", {})
    agents = data.get("agents", {})
    return Config(
        agents_approve=bool(agents.get("approve_on_pad", True)),
        agents_approve_wait_s=float(agents.get("approve_wait_s", Config.agents_approve_wait_s)),
        agents_keep_awake=bool(agents.get("keep_awake", True)),
        serial_glob=str(serial.get("glob", Config.serial_glob)),
        serial_port=(str(serial["port"]) if serial.get("port") else None),
        serial_baud=int(serial.get("baud", Config.serial_baud)),
        reconnect_s=float(serial.get("reconnect_s", Config.reconnect_s)),
        ping_interval_s=float(serial.get("ping_interval_s", Config.ping_interval_s)),
        log_level=str(daemon.get("log_level", Config.log_level)).upper(),
    )


def config_path() -> Path:
    env = os.environ.get("MACROPADD_CONFIG")
    return Path(env).expanduser() if env else DEFAULT_PATH.expanduser()


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return from_dict({})
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return from_dict(data)
