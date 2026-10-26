import queue

from macropadd.serial_transport import SerialTransport


def test_open_bumps_generation_and_drops_stale_events(monkeypatch):
    t = SerialTransport(port="/dev/null-not-real")
    # simulate a dead connection's thread reporting late
    t.events.put(("closed", "old thread died", 0))

    class FakeSerial:
        is_open = True
        def __init__(self, *a, **k): pass
        def reset_input_buffer(self): pass
        def read(self, n): return b""
        def close(self): self.is_open = False
    monkeypatch.setattr("macropadd.serial_transport.serial.Serial", FakeSerial)
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    assert t.open()
    assert t.generation == 1
    with __import__("pytest").raises(queue.Empty):
        t.events.get_nowait()          # stale event was drained on open


def test_lone_port_is_never_the_data_port(monkeypatch):
    from macropadd import serial_transport as st

    class Info:
        def __init__(self, device): self.device, self.vid, self.location = device, st.ADAFRUIT_VID, None
    monkeypatch.setattr(st.list_ports, "comports", lambda: [Info("/dev/cu.usbmodem1101")])
    assert st.candidate_ports() == []
    monkeypatch.setattr(st.list_ports, "comports",
                        lambda: [Info("/dev/cu.usbmodem1103"), Info("/dev/cu.usbmodem1101")])
    assert st.candidate_ports() == ["/dev/cu.usbmodem1103"]


def test_linux_orders_ports_by_usb_interface(monkeypatch):
    from macropadd import serial_transport as st

    class Info:
        def __init__(self, device, location):
            self.device, self.vid, self.location = device, st.ADAFRUIT_VID, location
    # ttyACM numbers follow probe order: here the data port got the lower one
    monkeypatch.setattr(st, "IS_MAC", False)
    monkeypatch.setattr(st.list_ports, "comports",
                        lambda: [Info("/dev/ttyACM0", "1-5.4:1.2"), Info("/dev/ttyACM1", "1-5.4:1.0"),
                                 type("Other", (), {"device": "/dev/ttyS0", "vid": None, "location": None})()])
    assert st.board_ports() == ["/dev/ttyACM1", "/dev/ttyACM0"]
    assert st.candidate_ports() == ["/dev/ttyACM0"]
