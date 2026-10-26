"""Install the AGENTS hooks into Claude Code and Codex.

    macropadd agents install      both agents (or: install claude / install codex)
    macropadd agents uninstall
    macropadd agents status       which hooks are in place

Claude: entries in ~/.claude/settings.json under "hooks", recognised by
their command (`-m macropadd.hook`), so reinstalling replaces them and
everything else in the file is left alone. Plus a `statusLine` command
that hands Claude's subscription limits (5 h / week) to the daemon and
prints model, context, limits and voice mode; it is only added when you
have no status line of your own, and refreshes every STATUS_REFRESH_S
(toggling /voice does not re-run a status line by itself).

Codex: one managed block of [[hooks.<Event>]] tables at the end of
~/.codex/config.toml, plus `[features] hooks = true`. Codex skips hooks
nobody trusted, so install asks `codex app-server` for each new hook's
hash (hooks/list) and records it under [hooks.state."<key>"], the same
thing the /hooks screen does when you trust a hook by hand.

Both files are backed up (`<file>.macropadd-<stamp>`) before a change.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MARKER = "macropadd.hook"

CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                 "PermissionRequest", "Notification", "PreCompact", "PostCompact", "SubagentStop",
                 "Stop", "SessionEnd")
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
                "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop", "Interrupt")

QUICK_TIMEOUT_S = 10
STATUS_REFRESH_S = 10
CODEX_INTERRUPT_TIMEOUT_S = 3            # Codex caps Interrupt hooks at 3 s
BLOCK_START = "# >>> macropadd agents: managed by `macropadd agents install`, do not edit >>>"
BLOCK_END = "# <<< macropadd agents <<<"

CLAUDE_SETTINGS = Path("~/.claude/settings.json")
CODEX_CONFIG = Path("~/.codex/config.toml")
TRUST_RECORD = Path("~/.local/state/macropadd/codex-trusted.json")


@dataclass
class Result:
    agent: str
    path: Path
    changed: bool
    note: str = ""

    def __str__(self) -> str:
        state = "updated" if self.changed else "unchanged"
        return f"{self.agent}: {state} {self.path}" + (f"\n  {self.note}" if self.note else "")


def hook_command(provider: str, python: str | None = None) -> str:
    # -P: never put the agent's working directory on sys.path (a project's
    # json.py must not shadow the stdlib). `|| true`: a missing venv must not
    # turn into a hook error in the agent.
    return f"{shlex.quote(python or sys.executable)} -P -m {MARKER} {provider} || true"


def permission_timeout(wait_s: float) -> int:
    return int(wait_s) + 30


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.macropadd-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


# ------------------------------------------------------------------- claude
def _ours(entry: object) -> bool:
    return isinstance(entry, dict) and any(
        MARKER in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def claude_settings(data: dict, command: str | None, wait_s: float = 30.0) -> dict:
    """`data` with our hooks replaced (command None: removed). Pure."""
    out = dict(data)
    hooks = dict(out.get("hooks") or {})
    for event in set(hooks) | set(CLAUDE_EVENTS):
        entries = [e for e in hooks.get(event, []) if not _ours(e)]
        if command and event in CLAUDE_EVENTS:
            timeout = permission_timeout(wait_s) if event == "PermissionRequest" else QUICK_TIMEOUT_S
            entries.append({"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": timeout}]})
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    status = out.get("statusLine")
    ours = isinstance(status, dict) and MARKER in str(status.get("command", ""))
    if command and (status is None or ours):
        out["statusLine"] = {"type": "command", "command": command.replace(" || true", " status || true"),
                             "refreshInterval": STATUS_REFRESH_S}
    elif not command and ours:
        out.pop("statusLine")
    return out


def install_claude(command: str | None, wait_s: float = 30.0, path: Path | None = None) -> Result:
    path = (path or CLAUDE_SETTINGS).expanduser()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON ({exc}); fix it first") from exc
    new = claude_settings(data, command, wait_s)
    if new == data:
        return Result("claude", path, False)
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = backup(path)
    path.write_text(json.dumps(new, indent=2) + "\n")
    return Result("claude", path, True, f"backup {saved}" if saved else "")


def claude_installed(path: Path | None = None) -> list[str]:
    path = (path or CLAUDE_SETTINGS).expanduser()
    try:
        hooks = json.loads(path.read_text()).get("hooks") or {}
    except (OSError, ValueError):
        return []
    return [e for e in CLAUDE_EVENTS if any(_ours(x) for x in hooks.get(e, []))]


# -------------------------------------------------------------------- codex
def strip_block(text: str) -> str:
    pattern = re.compile(r"\n*" + re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL)
    return pattern.sub("\n", text).rstrip("\n") + ("\n" if text.strip() else "")


def codex_block(command: str, wait_s: float = 30.0) -> str:
    lines = [BLOCK_START]
    for event in CODEX_EVENTS:
        timeout = (permission_timeout(wait_s) if event == "PermissionRequest"
                   else CODEX_INTERRUPT_TIMEOUT_S if event == "Interrupt" else QUICK_TIMEOUT_S)
        lines += [f"[[hooks.{event}]]", 'matcher = "*"', f"[[hooks.{event}.hooks]]", 'type = "command"',
                  f"command = '''{command}'''", f"timeout = {timeout}", ""]
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def ensure_hooks_feature(text: str) -> str:
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(r"\s*\[features\]\s*$", l)), None)
    if start is None:
        return text.rstrip("\n") + ("\n\n" if text.strip() else "") + "[features]\nhooks = true\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    for i in range(start + 1, end):
        if re.match(r"\s*hooks\s*=", lines[i]):
            lines[i] = "hooks = true\n"
            return "".join(lines)
    lines.insert(start + 1, "hooks = true\n")
    return "".join(lines)


def set_trusted(text: str, key: str, digest: str | None) -> str:
    """Add, replace or (digest None) remove one [hooks.state."key"] table."""
    header = f'[hooks.state."{key}"]'
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        del lines[start:end]
        text = "".join(lines)
    if digest is None:
        return re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip("\n") + f'\n\n{header}\ntrusted_hash = "{digest}"\n'


def codex_hooks(config: Path, codex: str | None = None, timeout_s: float = 10.0) -> list[dict]:
    """hooks/list from `codex app-server` for this config's hooks."""
    codex = codex or shutil.which("codex")
    if not codex:
        return []
    env = dict(os.environ, CODEX_HOME=str(config.parent))
    try:
        proc = subprocess.Popen([codex, "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, env=env, cwd=str(config.parent))
    except OSError:
        return []
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()  # type: ignore[union-attr]

    def call(msg_id: int, method: str, params: dict) -> dict | None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                msg = json.loads(lines.get(timeout=0.2))
            except (queue.Empty, ValueError):
                continue
            if msg.get("id") == msg_id:
                return msg
        return None

    try:
        call(1, "initialize", {"clientInfo": {"name": "macropadd", "version": "0"}, "capabilities": None})
        reply = call(2, "hooks/list", {"cwds": [str(config.parent)]})
    except OSError:
        reply = None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        hooks = reply["result"]["data"][0]["hooks"]   # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return []
    src = str(config)
    return [h for h in hooks if isinstance(h, dict) and h.get("sourcePath") == src and MARKER in str(h.get("command"))]


def _trust_record() -> dict[str, list[str]]:
    try:
        return json.loads(TRUST_RECORD.expanduser().read_text())
    except (OSError, ValueError):
        return {}


def _save_trust_record(record: dict[str, list[str]]) -> None:
    path = TRUST_RECORD.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1) + "\n")


def install_codex(command: str | None, wait_s: float = 30.0, path: Path | None = None,
                  lister=codex_hooks) -> Result:
    path = (path or CODEX_CONFIG).expanduser()
    original = path.read_text() if path.exists() else ""
    text = strip_block(original)
    record = _trust_record()
    for key in record.pop(str(path), []):          # trust we recorded for the old block
        text = set_trusted(text, key, None)
    if command:
        text = ensure_hooks_feature(text)
        text = text.rstrip("\n") + "\n\n" + codex_block(command, wait_s)
    changed = text != original
    saved = None
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        saved = backup(path)
        path.write_text(text)
    note = f"backup {saved}" if saved else ""
    if command:
        hooks = lister(path)
        keys = []
        for h in hooks:
            if isinstance(h.get("key"), str) and isinstance(h.get("currentHash"), str):
                text = set_trusted(text, h["key"], h["currentHash"])
                keys.append(h["key"])
        if keys:
            path.write_text(text)
            record[str(path)] = keys
            changed = True
            note = (note + "\n  " if note else "") + f"trusted {len(keys)} hooks; restart Codex to load them"
        else:
            note = (note + "\n  " if note else "") + "could not trust the hooks automatically: run /hooks in Codex and trust them"
    _save_trust_record(record)
    return Result("codex", path, changed, note)


def codex_installed(path: Path | None = None) -> bool:
    path = (path or CODEX_CONFIG).expanduser()
    try:
        return BLOCK_START in path.read_text()
    except OSError:
        return False


# ---------------------------------------------------------------------- cli
def install(which: str = "all", wait_s: float = 30.0, python: str | None = None) -> list[Result]:
    out = []
    if which in ("all", "claude"):
        out.append(install_claude(hook_command("claude", python), wait_s))
    if which in ("all", "codex"):
        out.append(install_codex(hook_command("codex", python), wait_s))
    return out


def uninstall(which: str = "all") -> list[Result]:
    out = []
    if which in ("all", "claude"):
        out.append(install_claude(None))
    if which in ("all", "codex"):
        out.append(install_codex(None))
    return out


def status() -> str:
    claude = claude_installed()
    lines = [f"claude: {'%d/%d hooks' % (len(claude), len(CLAUDE_EVENTS)) if claude else 'not installed'}"
             f" ({CLAUDE_SETTINGS})",
             f"codex:  {'installed' if codex_installed() else 'not installed'} ({CODEX_CONFIG})"]
    if codex_installed():
        hooks = codex_hooks(CODEX_CONFIG.expanduser())
        if hooks:
            untrusted = [h["eventName"] for h in hooks if h.get("trustStatus") != "trusted"]
            lines.append("        " + (f"untrusted: {', '.join(untrusted)} (run /hooks in Codex)" if untrusted
                                       else f"{len(hooks)} hooks trusted"))
    return "\n".join(lines)
