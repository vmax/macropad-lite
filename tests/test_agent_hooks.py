import json

from macropadd import agent_hooks as AH

CMD = "/venv/bin/python -P -m macropadd.hook claude || true"


def test_command_is_safe_and_fail_open():
    cmd = AH.hook_command("codex", "/v env/python")
    assert cmd == "'/v env/python' -P -m macropadd.hook codex || true"


def test_claude_settings_add_replace_remove():
    user = {"theme": "dark", "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say hi"}]}]}}
    new = AH.claude_settings(user, CMD, wait_s=30)
    assert new["theme"] == "dark"
    assert set(new["hooks"]) == set(AH.CLAUDE_EVENTS)
    assert new["hooks"]["Stop"][0]["hooks"][0]["command"] == "say hi"      # user's hook kept, first
    assert new["hooks"]["Stop"][1]["hooks"][0] == {"type": "command", "command": CMD, "timeout": AH.QUICK_TIMEOUT_S}
    assert new["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 60
    again = AH.claude_settings(new, CMD.replace("/venv", "/other"), wait_s=30)
    assert len(again["hooks"]["Stop"]) == 2 and "/other" in again["hooks"]["Stop"][1]["hooks"][0]["command"]
    assert AH.claude_settings(again, None) == user


def test_install_claude_writes_backup(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "dark"}))
    r = AH.install_claude(CMD, path=path)
    assert r.changed and "backup" in r.note
    assert AH.claude_installed(path) == list(AH.CLAUDE_EVENTS)
    assert not AH.install_claude(CMD, path=path).changed
    AH.install_claude(None, path=path)
    assert json.loads(path.read_text()) == {"theme": "dark"}
    assert len(list(tmp_path.glob("settings.json.macropadd-*"))) >= 1


def test_codex_block_and_feature_flag():
    block = AH.codex_block(CMD, wait_s=30)
    assert block.startswith(AH.BLOCK_START) and block.rstrip().endswith(AH.BLOCK_END)
    assert block.count("[[hooks.") == 2 * len(AH.CODEX_EVENTS)
    assert "timeout = 60" in block and "timeout = 3" in block
    assert AH.ensure_hooks_feature('model = "x"\n') == 'model = "x"\n\n[features]\nhooks = true\n'
    assert AH.ensure_hooks_feature("[features]\nother = 1\n[x]\n") == "[features]\nhooks = true\nother = 1\n[x]\n"
    assert AH.ensure_hooks_feature("[features]\nhooks = false\n") == "[features]\nhooks = true\n"


def test_trust_tables_add_and_remove():
    text = 'model = "x"\n'
    text = AH.set_trusted(text, "/c/config.toml:stop:0:0", "sha256:aa")
    assert '[hooks.state."/c/config.toml:stop:0:0"]\ntrusted_hash = "sha256:aa"' in text
    text = AH.set_trusted(text, "/c/config.toml:stop:0:0", "sha256:bb")
    assert text.count("hooks.state") == 1 and "sha256:bb" in text
    assert AH.set_trusted(text, "/c/config.toml:stop:0:0", None).strip() == 'model = "x"'


def test_install_codex_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(AH, "TRUST_RECORD", tmp_path / "trusted.json")
    path = tmp_path / "config.toml"
    original = 'model = "gpt-5"\n\n[features]\nfoo = true\n'
    path.write_text(original)

    def lister(p):
        return [{"key": f"{p}:stop:0:0", "currentHash": "sha256:1", "command": CMD}]

    r = AH.install_codex(CMD, path=path, lister=lister)
    text = path.read_text()
    assert r.changed and "trusted 1 hooks" in r.note
    assert "hooks = true" in text and AH.BLOCK_START in text and 'trusted_hash = "sha256:1"' in text
    AH.install_codex(CMD, path=path, lister=lister)          # reinstall: one block, one trust table
    text = path.read_text()
    assert text.count(AH.BLOCK_START) == 1 and text.count("hooks.state") == 1
    AH.install_codex(None, path=path)
    text = path.read_text()
    assert AH.BLOCK_START not in text and "hooks.state" not in text
    assert text.startswith('model = "gpt-5"') and "foo = true" in text


def test_install_codex_without_codex_cli_says_how_to_trust(tmp_path, monkeypatch):
    monkeypatch.setattr(AH, "TRUST_RECORD", tmp_path / "trusted.json")
    r = AH.install_codex(CMD, path=tmp_path / "config.toml", lister=lambda p: [])
    assert "/hooks" in r.note


def test_claude_status_line_added_only_when_free():
    new = AH.claude_settings({}, CMD)
    assert new["statusLine"] == {"type": "command", "command": CMD.replace(" || true", " status || true"),
                                 "refreshInterval": AH.STATUS_REFRESH_S}
    assert "statusLine" not in AH.claude_settings(new, None)
    mine = {"statusLine": {"type": "command", "command": "~/.claude/statusline.sh"}}
    assert AH.claude_settings(mine, CMD)["statusLine"] == mine["statusLine"]        # yours stays
    assert AH.claude_settings(mine, None) == mine
