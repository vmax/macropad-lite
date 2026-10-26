from pathlib import Path

import pytest

from macropadd import config as C


def test_defaults_without_file(tmp_path):
    cfg = C.load(tmp_path / "missing.toml")
    assert cfg.agents_approve and cfg.agents_approve_wait_s == 30.0
    assert cfg.serial_glob == ("/dev/cu.usbmodem*" if C.IS_MAC else "/dev/ttyACM*")


def test_example_config_loads():
    cfg = C.load(Path(__file__).resolve().parents[1] / "macropadd.example.toml")
    assert cfg.serial_baud == 115200 and cfg.agents_keep_awake


def test_agents_section(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[agents]\napprove_on_pad = false\napprove_wait_s = 5\n")
    cfg = C.load(p)
    assert not cfg.agents_approve and cfg.agents_approve_wait_s == 5.0


def test_malformed_toml_is_error(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("[serial\n")
    with pytest.raises(C.ConfigError):
        C.load(p)


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MACROPADD_CONFIG", str(tmp_path / "x.toml"))
    assert C.config_path() == tmp_path / "x.toml"
