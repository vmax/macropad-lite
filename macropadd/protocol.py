"""NDJSON protocol codec and message builders (host side).

Mirrors firmware/protocol.py. One JSON object per line, "type" on every
message; unknown types are ignored, malformed lines decode to None.
"""

from __future__ import annotations

import json
from typing import Any

from . import PROTOCOL, __version__

MAX_LINE = 4096
Message = dict[str, Any]


def encode(msg: Message) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> Message | None:
    if not line or len(line) > MAX_LINE:
        return None
    try:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8")
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        return None
    return msg


# ------------------------------------------------------- daemon -> device
def hello() -> Message:
    return {"type": "hello", "protocol": PROTOCOL, "daemon_version": __version__}


def time_msg(unix: float, tz_offset_s: int) -> Message:
    return {"type": "time", "unix": int(unix), "tz_offset_s": int(tz_offset_s)}


def ping(seq: int) -> Message:
    return {"type": "ping", "seq": seq}


def overlay(text: str, ttl_ms: int = 1200) -> Message:
    return {"type": "overlay", "text": text, "ttl_ms": ttl_ms}


def agents_state(agents: list[dict], more: int = 0, usage: dict | None = None) -> Message:
    """AGENTS rows: k slot (key), p provider letter, n name, m mode, s seconds in
    mode, t title, d detail, q pending permission id (answer with agent_decide).
    usage: {"C"|"X": [% 5h, s to reset, % week, s to reset]}, -1 = unknown."""
    msg: Message = {"type": "agents_state", "agents": agents, "more": more}
    if usage:
        msg["u"] = usage
    return msg


def error(msg: str) -> Message:
    return {"type": "error", "msg": msg}
