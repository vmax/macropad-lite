"""Run `macropadd run` at login: a LaunchAgent on macOS, a systemd user unit on Linux.

    macropadd agent install     write plist / unit, load, start
    macropadd agent uninstall   unload, remove plist / unit
    macropadd agent status      launchctl print / systemctl status summary
    macropadd agent restart     kill and relaunch (picks up edited code)

Both point at the current Python interpreter (`-m macropadd run`), so
install from the environment you want the agent to use, e.g.
`uv run macropadd agent install` from the repository.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import IS_MAC

LABEL = "com.macropad.macropadd"
PLIST_PATH = Path("~/Library/LaunchAgents") / f"{LABEL}.plist"
LOG_PATH = Path("~/Library/Logs/macropadd.log")

UNIT = "macropadd.service"
UNIT_PATH = Path("~/.config/systemd/user") / UNIT


# ------------------------------------------------------------------ macOS
def plist_dict(python: str, log_path: Path, config_path: str | None = None) -> dict:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"}
    if config_path:
        env["MACROPADD_CONFIG"] = config_path
    return {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "macropadd", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": env,
        "ProcessType": "Background",
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _mac_install(python: str, plist_path: Path) -> str:
    log_path = LOG_PATH.expanduser()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    data = plist_dict(python, log_path, os.environ.get("MACROPADD_CONFIG"))
    if plist_path.exists():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    with plist_path.open("wb") as fh:
        plistlib.dump(data, fh)
    res = _launchctl("bootstrap", _domain(), str(plist_path))
    if res.returncode != 0:
        return f"wrote {plist_path}\nlaunchctl bootstrap failed: {res.stderr.strip() or res.stdout.strip()}"
    _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    return f"installed {plist_path}\nlogs: {log_path}"


def _mac_uninstall(plist_path: Path) -> str:
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    if plist_path.exists():
        plist_path.unlink()
        return f"removed {plist_path}"
    return "not installed"


def _mac_restart() -> str:
    res = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    if res.returncode != 0:
        return f"restart failed: {res.stderr.strip() or res.stdout.strip() or 'not installed'}"
    return "restarted\n" + status()


def _mac_status() -> str:
    res = _launchctl("print", f"{_domain()}/{LABEL}")
    if res.returncode != 0:
        return "not loaded"
    keep = [l.strip() for l in res.stdout.splitlines()
            if any(k in l for k in ("state =", "pid =", "last exit code", "program ="))]
    return "\n".join(keep) or res.stdout.strip()[:400]


# ------------------------------------------------------------------ Linux
def unit_text(python: str, config_path: str | None = None) -> str:
    """A user unit tied to the graphical session: the clipboard (wl-copy /
    xclip) and the keyring need WAYLAND_DISPLAY / DISPLAY and the session bus."""
    env = f"Environment=MACROPADD_CONFIG={config_path}\n" if config_path else ""
    return (
        "[Unit]\n"
        "Description=MacroPad desk terminal daemon\n"
        "PartOf=graphical-session.target\n"
        "After=graphical-session.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={python} -m macropadd run\n"
        f"{env}"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical-session.target\n"
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _linux_install(python: str, unit_path: Path) -> str:
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_text(python, os.environ.get("MACROPADD_CONFIG")))
    _systemctl("daemon-reload")
    res = _systemctl("enable", "--now", UNIT)
    if res.returncode != 0:
        return f"wrote {unit_path}\nsystemctl enable failed: {res.stderr.strip() or res.stdout.strip()}"
    _systemctl("restart", UNIT)           # pick up a changed unit on reinstall
    return f"installed {unit_path}\nlogs: journalctl --user -u {UNIT} -f"


def _linux_uninstall(unit_path: Path) -> str:
    _systemctl("disable", "--now", UNIT)
    if unit_path.exists():
        unit_path.unlink()
        _systemctl("daemon-reload")
        return f"removed {unit_path}"
    return "not installed"


def _linux_restart() -> str:
    res = _systemctl("restart", UNIT)
    if res.returncode != 0:
        return f"restart failed: {res.stderr.strip() or res.stdout.strip() or 'not installed'}"
    return "restarted\n" + status()


def _linux_status() -> str:
    res = _systemctl("show", UNIT, "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStatus")
    props = dict(l.split("=", 1) for l in res.stdout.splitlines() if "=" in l)
    if res.returncode != 0 or props.get("LoadState") in (None, "not-found"):
        return "not loaded"
    return (f"state = {props.get('ActiveState')} ({props.get('SubState')})\n"
            f"pid = {props.get('MainPID')}\nlast exit code = {props.get('ExecMainStatus')}")


# ----------------------------------------------------------------- public
def install(python: str | None = None, path: Path | None = None) -> str:
    python = python or sys.executable
    if IS_MAC:
        return _mac_install(python, (path or PLIST_PATH).expanduser())
    return _linux_install(python, (path or UNIT_PATH).expanduser())


def uninstall(path: Path | None = None) -> str:
    if IS_MAC:
        return _mac_uninstall((path or PLIST_PATH).expanduser())
    return _linux_uninstall((path or UNIT_PATH).expanduser())


def restart() -> str:
    """Kill the running daemon and let launchd / systemd start it again.

    The install is editable, so a restart is all a code change needs.
    """
    return _mac_restart() if IS_MAC else _linux_restart()


def status() -> str:
    return _mac_status() if IS_MAC else _linux_status()
