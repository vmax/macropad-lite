import plistlib
from pathlib import Path

from macropadd import agent


def test_plist_dict_shape():
    d = agent.plist_dict("/usr/bin/python3", Path("/tmp/x.log"), "/tmp/cfg.toml")
    assert d["Label"] == agent.LABEL
    assert d["ProgramArguments"] == ["/usr/bin/python3", "-m", "macropadd", "run"]
    assert d["KeepAlive"] is True and d["RunAtLoad"] is True
    assert d["EnvironmentVariables"]["MACROPADD_CONFIG"] == "/tmp/cfg.toml"
    assert d["StandardOutPath"] == "/tmp/x.log"
    plistlib.dumps(d)          # serialisable


def test_plist_without_config_env():
    d = agent.plist_dict("/usr/bin/python3", Path("/tmp/x.log"))
    assert "MACROPADD_CONFIG" not in d["EnvironmentVariables"]


def test_restart_uses_kickstart(monkeypatch):
    monkeypatch.setattr(agent, "IS_MAC", True)
    calls = []

    class Res:
        returncode = 0
        stdout = "state = running\n"
        stderr = ""

    monkeypatch.setattr(agent, "_launchctl", lambda *a: calls.append(a) or Res())
    out = agent.restart()
    assert calls[0][:2] == ("kickstart", "-k")
    assert calls[0][2].endswith(agent.LABEL)
    assert out.startswith("restarted")


def test_restart_reports_failure(monkeypatch):
    monkeypatch.setattr(agent, "IS_MAC", True)

    class Res:
        returncode = 113
        stdout = ""
        stderr = "Could not find service"

    monkeypatch.setattr(agent, "_launchctl", lambda *a: Res())
    assert "Could not find service" in agent.restart()


def test_unit_text_shape():
    text = agent.unit_text("/usr/bin/python3", "/tmp/cfg.toml")
    assert "ExecStart=/usr/bin/python3 -m macropadd run\n" in text
    assert "Environment=MACROPADD_CONFIG=/tmp/cfg.toml\n" in text
    assert "Restart=always" in text and "WantedBy=graphical-session.target" in text
    assert "MACROPADD_CONFIG" not in agent.unit_text("/usr/bin/python3")


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_linux_install_and_uninstall(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "IS_MAC", False)
    monkeypatch.delenv("MACROPADD_CONFIG", raising=False)
    calls = []
    monkeypatch.setattr(agent, "_systemctl", lambda *a: calls.append(a) or _Res())
    unit = tmp_path / "systemd" / "user" / agent.UNIT
    out = agent.install("/venv/bin/python", unit)
    assert unit.read_text() == agent.unit_text("/venv/bin/python")
    assert ("enable", "--now", agent.UNIT) in calls and "journalctl" in out
    assert agent.uninstall(unit).startswith("removed") and not unit.exists()
    assert ("disable", "--now", agent.UNIT) in calls


def test_linux_status(monkeypatch):
    monkeypatch.setattr(agent, "IS_MAC", False)
    show = "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=42\nExecMainStatus=0\n"
    monkeypatch.setattr(agent, "_systemctl", lambda *a: _Res(stdout=show))
    assert agent.status() == "state = active (running)\npid = 42\nlast exit code = 0"
    monkeypatch.setattr(agent, "_systemctl", lambda *a: _Res(stdout="LoadState=not-found\n"))
    assert agent.status() == "not loaded"
