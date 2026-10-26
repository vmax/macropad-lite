"""USB CDC transport: find the MacroPad data port, read lines, reconnect.

A reader thread pushes ("line", bytes, gen) and ("closed", reason, gen)
into a queue; the daemon loop drains it and ignores events whose
generation is not the current connection's (a thread from a dead
connection may report its death after the next one is already open).
write() is called from the main thread.

Port selection: CircuitPython exposes two CDC interfaces. macOS names
them /dev/cu.usbmodem<location><n>; the console gets the lower number,
the data channel the next one. Linux names them /dev/ttyACM<n> in probe
order, so there we sort by USB location instead (`1-5.4:1.0` console,
`1-5.4:1.2` data). We filter by Adafruit's USB vendor id (0x239A) and
take the second port of the pair. `serial.port` in the config overrides
all of that.
"""

from __future__ import annotations

import glob
import logging
import queue
import threading
import time

import serial
from serial.tools import list_ports

from .config import DEFAULT_GLOB, IS_MAC

log = logging.getLogger("macropadd.serial")

ADAFRUIT_VID = 0x239A


def board_ports(pattern: str = DEFAULT_GLOB) -> list[str]:
    """The board's CDC ports, console first."""
    infos = [i for i in list_ports.comports() if i.vid == ADAFRUIT_VID
             and (i.device.startswith("/dev/cu.") or not IS_MAC)]
    if infos:
        # macOS: the usbmodem number orders them. Linux: ttyACM numbers follow
        # probe order, so order by the interface in the USB location.
        infos.sort(key=lambda i: (i.device,) if IS_MAC else (i.location or "", i.device))
        return [i.device for i in infos]
    return sorted(glob.glob(pattern))


def candidate_ports(pattern: str = DEFAULT_GLOB) -> list[str]:
    """Data-port candidates, best first.

    The board exposes two CDC ports; the data channel is the second. With
    only one port visible we return nothing: the board is mid-enumeration
    or in safe mode, and opening its console as the data port would feed
    JSON into the firmware's command console (which reacts to 'R', 'm',
    'p' ...). Wait for the second port instead.
    """
    ports = board_ports(pattern)
    if len(ports) < 2:
        return []
    return [ports[1]]


class SerialTransport:
    def __init__(self, port: str | None, baud: int = 115200, pattern: str = DEFAULT_GLOB):
        self._fixed_port = port
        self._baud = baud
        self._pattern = pattern
        self._ser: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self.events: queue.Queue[tuple[str, object, int]] = queue.Queue()
        self.port: str | None = None
        self.generation = 0

    # ------------------------------------------------------------ open
    def open(self) -> bool:
        ports = [self._fixed_port] if self._fixed_port else candidate_ports(self._pattern)
        for port in ports:
            try:
                # exclusive: a second macropadd (or tio) on the data port fails
                # loudly instead of both silently splitting the byte stream.
                ser = serial.Serial(port, self._baud, timeout=0.5, write_timeout=1.0, exclusive=True)
            except (serial.SerialException, OSError) as exc:
                log.warning("open %s failed: %s", port, str(exc).splitlines()[0])
                continue
            try:
                ser.reset_input_buffer()   # stale bytes from before we opened
            except (serial.SerialException, OSError):
                pass
            self._ser = ser
            self.port = port
            self.generation += 1
            while not self.events.empty():        # stale events from the last connection
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break
            self._thread = threading.Thread(target=self._reader, args=(ser, self.generation),
                                            daemon=True, name="serial-reader-%d" % self.generation)
            self._thread.start()
            return True
        return False

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------- io
    def write(self, data: bytes) -> bool:
        ser = self._ser
        if ser is None:
            return False
        try:
            ser.write(data)
            return True
        except (serial.SerialException, OSError) as exc:
            log.warning("write failed: %s", exc)
            self.events.put(("closed", str(exc), self.generation))
            return False

    def _reader(self, ser: serial.Serial, gen: int) -> None:
        buf = bytearray()
        try:
            while ser.is_open and ser is self._ser:
                chunk = ser.read(256)
                if not chunk:
                    continue
                buf += chunk
                while True:
                    idx = buf.find(b"\n")
                    if idx < 0:
                        break
                    line = bytes(buf[:idx]).rstrip(b"\r")
                    del buf[: idx + 1]
                    if line:
                        self.events.put(("line", line, gen))
                if len(buf) > 8192:
                    buf.clear()
        except (serial.SerialException, OSError, TypeError) as exc:
            self.events.put(("closed", str(exc), gen))
            return
        self.events.put(("closed", "port closed", gen))


def wait_for_port(transport: SerialTransport, interval_s: float, stop: threading.Event) -> bool:
    """Try to open until it works or stop is set."""
    announced = False
    while not stop.is_set():
        if transport.open():
            return True
        if not announced:
            n = len(board_ports(transport._pattern))
            hint = " (one port only: board booting or in safe mode)" if n == 1 else ""
            log.info("waiting for device (%s)%s", transport._fixed_port or transport._pattern, hint)
            announced = True
        time.sleep(interval_s)
    return False
