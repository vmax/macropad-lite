"""Agent hook entry: `python -P -m macropadd.hook claude|codex`
(and `claude status` as Claude's status line command: it hands the rate
limits to the daemon and prints the status line, see status_line()).

Claude Code and Codex run this on every hook event with the event JSON
on stdin. It forwards the event to the daemon over a Unix socket and
exits 0 whatever happens: a missing daemon must never slow down or
break an agent.

PermissionRequest is the one event that waits. The daemon shows the
request on the pad and answers allow / deny, or nothing (pad offline,
approvals off, timeout, "ask in the terminal"). With an answer we print
the decision JSON both agents understand; without one we print nothing
and the agent shows its normal approval prompt.

Stdlib only and import-light: this runs before every tool call.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

CONNECT_TIMEOUT_S = 0.5
WAIT_CAP_S = 3600.0       # the daemon decides how long; this only bounds a hung socket
DENY_MESSAGE = "Denied from the MacroPad."


def socket_path() -> Path:
    env = os.environ.get("MACROPADD_AGENTS_SOCKET")
    if env:
        return Path(env).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "macropadd" / "agents.sock"
    return Path("~/.local/state/macropadd/agents.sock").expanduser()


def decision_output(decision: str, permissions: list | None = None) -> dict:
    out: dict = {"behavior": decision}
    if decision == "deny":
        out["message"] = DENY_MESSAGE
    elif permissions:
        out["updatedPermissions"] = permissions   # "Yes, and don't ask again" (Claude only)
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}}


def _readline(sock: socket.socket) -> bytes:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0]


def forward(provider: str, event: dict, path: Path | None = None) -> tuple[str | None, list]:
    """Send one event; for PermissionRequest return the pad's decision and,
    for "don't ask again", the permission updates to apply."""
    wait = event.get("hook_event_name") == "PermissionRequest"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.connect(str(path or socket_path()))
        sock.sendall(json.dumps({"provider": provider, "event": event, "wait": wait},
                                separators=(",", ":")).encode() + b"\n")
        if not wait:
            return None, []
        sock.settimeout(WAIT_CAP_S)
        reply = json.loads(_readline(sock) or b"{}")
    finally:
        sock.close()
    if not isinstance(reply, dict) or reply.get("decision") not in ("allow", "deny"):
        return None, []
    updates = reply.get("permissions")
    return reply["decision"], updates if isinstance(updates, list) else []


def forward_status(provider: str, status: dict, path: Path | None = None) -> None:
    """Claude's status line data (rate limits); fire and forget."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        sock.connect(str(path or socket_path()))
        sock.sendall(json.dumps({"provider": provider, "status": status}, separators=(",", ":")).encode() + b"\n")
    finally:
        sock.close()


def _pct(value: float) -> str:
    """'13%' coloured like the pad's usage key: green < 50 % < yellow < 80 % < red."""
    color = 32 if value < 50 else 33 if value < 80 else 31
    return f"\033[{color}m{value:.0f}%\033[0m"


def _until(resets_at: object, now: float) -> str:
    try:
        s = int(float(resets_at) - now)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600}h"


def voice_mode(settings: Path | None = None) -> str:
    """'hold' / 'tap' when /voice is on (it saves itself in the user settings), else ''."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    try:
        voice = json.loads((settings or base / "settings.json").read_text()).get("voice") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    return str(voice.get("mode") or "hold") if isinstance(voice, dict) and voice.get("enabled") else ""


def status_line(data: dict, now: float | None = None, voice: str | None = None) -> str:
    """Model · context · 5-hour and weekly limits with resets · voice mode."""
    now = time.time() if now is None else now
    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(str(model))
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if isinstance(ctx, (int, float)):
        parts.append("ctx " + _pct(ctx))
    limits = data.get("rate_limits") or {}
    for key, name in (("five_hour", "5h"), ("seven_day", "wk")):
        w = limits.get(key) if isinstance(limits, dict) else None
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
            left = _until(w.get("resets_at"), now)
            parts.append(f"{name} {_pct(w['used_percentage'])}" + (f" ({left})" if left else ""))
    voice = voice_mode() if voice is None else voice
    if voice:
        parts.append("\U0001f399 " + voice)
    return " \033[2m·\033[0m ".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    provider = args[0] if args else "claude"
    try:
        event = json.loads(sys.stdin.buffer.read() or b"{}")
        if not isinstance(event, dict):
            return 0
        if args[1:2] == ["status"]:
            # As Claude's status line: hand the limits to the pad, print the line.
            try:
                forward_status(provider, {"rate_limits": event.get("rate_limits")})
            except OSError:
                pass                          # no daemon: the line still shows
            sys.stdout.write(status_line(event) + "\n")
            return 0
        decision, updates = forward(provider, event)
    except (OSError, ValueError):
        return 0
    if decision:
        sys.stdout.write(json.dumps(decision_output(decision, updates)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
