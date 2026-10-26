import json

from macropadd import PROTOCOL
from macropadd import protocol as P


def test_encode_is_one_line_with_newline():
    data = P.encode(P.hello())
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1
    assert json.loads(data)["type"] == "hello"


def test_round_trip():
    for msg in (P.hello(), P.time_msg(1789290000.7, 10800), P.ping(3), P.overlay("HI"),
                P.agents_state([{"k": 0, "p": "C"}], usage={"C": [13, 600, -1, -1]}), P.error("e")):
        back = P.decode(P.encode(msg).rstrip(b"\n"))
        assert back == msg


def test_hello_carries_protocol_version():
    assert P.hello()["protocol"] == PROTOCOL


def test_time_is_integer_seconds():
    assert P.time_msg(1789290000.7, 10800)["unix"] == 1789290000


def test_malformed_lines_decode_to_none():
    for bad in (b"", b"{", b"not json", b"[1,2]", b'{"no_type":1}', b'{"type":5}', b"\xff\xfe", "x" * 5000):
        assert P.decode(bad) is None


def test_unknown_type_still_decodes():
    assert P.decode(b'{"type":"future","x":1}') == {"type": "future", "x": 1}


def test_device_examples_from_spec():
    lines = [
        b'{"type":"hello","protocol":1,"device":"macropad"}',
        b'{"type":"key","key":0,"event":"pressed","mode":"worklog"}',
        b'{"type":"worklog","stream":"R","action":"start"}',
        b'{"type":"encoder","delta":1,"mode":"desk"}',
        b'{"type":"action","name":"toggle_mute"}',
    ]
    types = [P.decode(l)["type"] for l in lines]
    assert types == ["hello", "key", "worklog", "encoder", "action"]
