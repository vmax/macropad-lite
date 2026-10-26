"""Protocol session: what to send when, independent of serial or SQLite.

The daemon feeds it three things and sends whatever comes back:

    out = session.on_connect(now)
    out = session.on_message(msg, now)
    out = session.on_tick(now)          # also check session.timed_out

Handlers for device-originated business messages (worklog, action,
worklog_query) are injected as callables returning a list of replies.
Keeping this pure makes reconnect/resync behaviour unit-testable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from . import PROTOCOL
from . import protocol as P

log = logging.getLogger("macropadd.session")

Message = dict[str, Any]
Handler = Callable[[Message, float], list[Message]]

LINK_TIMEOUT_S = 5.0
TIME_RESYNC_S = 3600.0


def local_tz_offset_s(unix: float | None = None) -> int:
    lt = time.localtime(unix)
    return lt.tm_gmtoff


class Session:
    def __init__(
        self,
        ping_interval_s: float = 2.0,
        wall_clock: Callable[[], float] = time.time,
        handlers: dict[str, Handler] | None = None,
        state_provider: Callable[[], list[Message]] | None = None,
    ):
        self._ping_interval = ping_interval_s
        self._wall = wall_clock
        self._handlers = handlers or {}
        self._state_provider = state_provider or (lambda: [])
        self.connected = False
        self.device_hello: Message | None = None
        self.awaiting_hello = False   # we greeted, device has not answered yet
        self.seq = 0
        self.last_rx: float | None = None
        self.last_ping = 0.0
        self.last_time_sync = 0.0
        self.rtt_ms: float | None = None
        self._ping_sent_at: dict[int, float] = {}
        self.stats = {"rx": 0, "tx": 0, "bad": 0, "unknown": 0}

    # ------------------------------------------------------------ events
    def on_connect(self, now: float) -> list[Message]:
        """Port opened: say hello only. The device may still be booting and its
        CDC receive buffer is small; the state bundle goes out once it answers."""
        self.connected = True
        self.device_hello = None
        self.last_rx = now
        self.last_ping = now
        self.last_time_sync = now      # time goes with the sync bundle, not before hello
        self._ping_sent_at.clear()
        return self._greet(now)

    def on_disconnect(self) -> None:
        self.connected = False
        self.device_hello = None

    def _greet(self, now: float) -> list[Message]:
        self.awaiting_hello = True
        return [P.hello()]

    def _sync_bundle(self, now: float) -> list[Message]:
        """Time plus authoritative state, sent whenever the device says hello."""
        self.last_time_sync = now
        unix = self._wall()
        return [P.time_msg(unix, local_tz_offset_s(unix))] + list(self._state_provider())

    def on_message(self, msg: Message | None, now: float) -> list[Message]:
        if msg is None:
            self.stats["bad"] += 1
            return []
        self.stats["rx"] += 1
        self.last_rx = now
        t = msg["type"]
        if t == "hello":
            self.device_hello = msg
            if msg.get("protocol") != PROTOCOL:
                log.warning("protocol mismatch: device %r, daemon %d", msg.get("protocol"), PROTOCOL)
            if self.awaiting_hello:
                self.awaiting_hello = False   # reply to our greeting
            else:
                # Unsolicited hello: the device (re)booted while the port stayed open.
                log.info("device announced itself (boot %s), resyncing", msg.get("boot_id"))
            return self._sync_bundle(now)
        if t == "pong":
            sent = self._ping_sent_at.pop(msg.get("seq", -1), None)
            if sent is not None:
                self.rtt_ms = (now - sent) * 1000.0
            return []
        if t == "log":
            log.info("device: %s", msg.get("msg", ""))
            return []
        handler = self._handlers.get(t)
        if handler is None:
            self.stats["unknown"] += 1
            log.debug("unknown message type %r", t)
            return []
        return handler(msg, now)

    def on_tick(self, now: float) -> list[Message]:
        out: list[Message] = []
        if not self.connected:
            return out
        if now - self.last_ping >= self._ping_interval:
            self.seq += 1
            self.last_ping = now
            self._ping_sent_at[self.seq] = now
            if len(self._ping_sent_at) > 8:
                oldest = min(self._ping_sent_at)
                del self._ping_sent_at[oldest]
            out.append(P.ping(self.seq))
        if now - self.last_time_sync >= TIME_RESYNC_S:
            self.last_time_sync = now
            unix = self._wall()
            out.append(P.time_msg(unix, local_tz_offset_s(unix)))
        return out

    def silent_for(self, now: float) -> float:
        return now - self.last_rx if self.last_rx is not None else 0.0

    def is_stale(self, now: float) -> bool:
        return self.connected and self.silent_for(now) > LINK_TIMEOUT_S
