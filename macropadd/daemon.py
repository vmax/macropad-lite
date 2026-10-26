"""Daemon main loop: transport + session + the AGENTS service.

    transport events  ->  session.on_message  ->  transport.write(replies)
    every 100 ms      ->  session.on_tick     ->  pings, hourly time
                          agents.tick         ->  hook events since the last pass
    silence > 5 s     ->  close, reconnect

Messages for the pad's other modes (worklog, action, otp ...) have no
handler here; the session logs and drops them.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import protocol as P
from .agents import AgentServer, AgentService, KeepAwake
from .config import Config
from .serial_transport import SerialTransport, wait_for_port
from .session import Message, Session

log = logging.getLogger("macropadd")

HELLO_TIMEOUT_S = 4.0   # a real data port answers hello well within this


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = threading.Event()
        self.agents = AgentService(
            approve_on_pad=cfg.agents_approve, approve_wait_s=cfg.agents_approve_wait_s,
            device_online=lambda: self.transport.is_open and self.session.device_hello is not None,
            keep_awake=KeepAwake(cfg.agents_keep_awake))
        self.agent_server: AgentServer | None = AgentServer(self.agents)
        self.transport = SerialTransport(cfg.serial_port, cfg.serial_baud, cfg.serial_glob)
        self.session = Session(
            ping_interval_s=cfg.ping_interval_s,
            handlers=self.agents.handlers(),
            state_provider=self.agents.state_messages,
        )

    # ------------------------------------------------------------ send
    def send(self, msgs: list[Message]) -> None:
        for i, msg in enumerate(msgs):
            if i:
                time.sleep(0.02)   # the board's CDC receive buffer is small; do not burst
            if self.transport.write(P.encode(msg)):
                self.session.stats["tx"] += 1
                log.debug("-> %s", msg)

    # ------------------------------------------------------------ loop
    def run(self) -> int:
        log.info("macropadd (lite) starting (protocol %d)", P.PROTOCOL)
        if not self.agent_server.start():
            self.agent_server = None
        try:
            while not self.stop.is_set():
                if not wait_for_port(self.transport, self.cfg.reconnect_s, self.stop):
                    break
                log.info("device connected on %s", self.transport.port)
                self._serve()
                self.transport.close()
                self.session.on_disconnect()
                self.agents.close()           # keep-awake follows the pad
                log.info("device disconnected")
                time.sleep(self.cfg.reconnect_s)
        except KeyboardInterrupt:
            pass
        self.transport.close()
        if self.agent_server is not None:
            self.agent_server.stop()
        self.agents.close()
        log.info("macropadd stopped")
        return 0

    def _serve(self) -> None:
        now = time.monotonic()
        connected_at = now
        self.send(self.session.on_connect(now))
        while not self.stop.is_set():
            try:
                kind, payload, gen = self.transport.events.get(timeout=0.1)
            except queue.Empty:
                kind, payload, gen = "tick", None, self.transport.generation
            now = time.monotonic()
            if gen != self.transport.generation:
                log.debug("ignoring stale %s event from connection %d", kind, gen)
                continue
            if kind == "closed":
                log.warning("port closed: %s", payload)
                return
            if kind == "line":
                msg = P.decode(payload)  # type: ignore[arg-type]
                if msg is None:
                    log.warning("bad line: %r", payload[:80])  # type: ignore[index]
                else:
                    log.debug("<- %s", msg)
                    if msg["type"] == "hello":
                        log.info("device hello fw=%s boot=%s; syncing time and state",
                                 msg.get("fw"), msg.get("boot_id"))
                self.send(self.session.on_message(msg, now))
            self.send(self.session.on_tick(now))
            if self.session.device_hello is not None:   # device is up and reading
                self.send(self.agents.tick(now))
            elif now - connected_at > HELLO_TIMEOUT_S:
                log.warning("no hello from the device in %.0fs; wrong port or console attached, reconnecting",
                            HELLO_TIMEOUT_S)
                return
            if self.session.is_stale(now):
                log.warning("no message for %.1fs, reconnecting", self.session.silent_for(now))
                return
