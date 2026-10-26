from macropadd import protocol as P
from macropadd.session import LINK_TIMEOUT_S, Session

WALL = 1789290000.0


def make(**kw):
    kw.setdefault("wall_clock", lambda: WALL)
    kw.setdefault("ping_interval_s", 2.0)
    return Session(**kw)


def types(msgs):
    return [m["type"] for m in msgs]


def test_connect_sends_hello_only_then_state_after_device_answers():
    s = make(state_provider=lambda: [P.agents_state([{"k": 0, "p": "C"}])])
    out = s.on_connect(now=10.0)
    assert types(out) == ["hello"]           # device may still be booting: no burst
    assert s.awaiting_hello
    out = s.on_message({"type": "hello", "protocol": 1, "boot_id": "a"}, 10.1)
    assert types(out) == ["time", "agents_state"]
    assert out[0]["unix"] == int(WALL)
    assert not s.awaiting_hello
    assert s.device_hello["boot_id"] == "a"


def test_unsolicited_hello_means_reboot_and_resyncs():
    s = make(state_provider=lambda: [P.agents_state([])])
    s.on_connect(10.0)
    s.on_message({"type": "hello", "protocol": 1, "boot_id": "a"}, 10.1)
    out = s.on_message({"type": "hello", "protocol": 1, "boot_id": "b"}, 50.0)
    assert types(out) == ["time", "agents_state"]
    assert not s.awaiting_hello


def test_ping_cadence_and_rtt():
    s = make()
    s.on_connect(0.0)
    assert s.on_tick(1.0) == []
    out = s.on_tick(2.0)
    assert types(out) == ["ping"] and out[0]["seq"] == 1
    assert s.on_tick(3.0) == []
    s.on_message({"type": "pong", "seq": 1}, 2.05)
    assert 40 < s.rtt_ms < 60
    assert types(s.on_tick(4.0)) == ["ping"]


def test_no_pings_when_disconnected():
    s = make()
    assert s.on_tick(100.0) == []


def test_stale_detection():
    s = make()
    s.on_connect(0.0)
    assert not s.is_stale(LINK_TIMEOUT_S - 0.1)
    assert s.is_stale(LINK_TIMEOUT_S + 0.1)
    s.on_message({"type": "pong", "seq": 0}, LINK_TIMEOUT_S)
    assert not s.is_stale(LINK_TIMEOUT_S + 1)
    s.on_disconnect()
    assert not s.is_stale(1000.0)


def test_bad_and_unknown_are_counted_not_raised():
    s = make()
    s.on_connect(0.0)
    assert s.on_message(None, 0.1) == []
    assert s.on_message({"type": "mystery"}, 0.2) == []
    assert s.stats["bad"] == 1 and s.stats["unknown"] == 1


def test_handlers_receive_business_messages():
    seen = []

    def on_worklog(msg, now):
        seen.append((msg["stream"], now))
        return [P.overlay("R STARTED")]

    s = make(handlers={"worklog": on_worklog})
    s.on_connect(0.0)
    out = s.on_message({"type": "worklog", "stream": "R", "action": "start"}, 1.0)
    assert seen == [("R", 1.0)]
    assert types(out) == ["overlay"]


def test_hourly_time_resync():
    s = make()
    s.on_connect(0.0)
    s.on_message({"type": "pong", "seq": 0}, 3599.0)   # keep it fresh
    out = s.on_tick(3600.5)
    assert "time" in types(out)
