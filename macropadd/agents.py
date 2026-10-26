"""AGENTS mode, host side: Claude Code / Codex sessions as glanceable states.

Modelled on SidePulse's agent monitor (github.com/inteliwear/sidepulse):
every hook event maps to a mode, each session keeps its latest mode,
modes expire after a while, and the most actionable one wins:

    error  blocked / a tool failed           red double blink
    wait   needs a decision, permission      amber pulse
    tool   a tool is running                 cyan roll
    work   thinking / generating             cyan roll
    done   turn finished                     solid green, 20 min
    idle   started or interrupted            not shown

Differences from SidePulse, for a pad with keys and a screen: one row
and one key per session (keys 0-6, stable while the session lives; key 7
is push-to-talk and, double-tapped, Enter; key 8 opens the usage page)
instead of a single aggregate; subagent events fold into their session;
SessionEnd removes the session instead of showing it green; and a
PermissionRequest can be answered from the pad (allow / deny / "ask in
the terminal").

Events arrive from `macropadd.hook` over a Unix socket (AgentServer).
The tracker is shared between the socket threads and the serial loop,
so every public method takes the lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol as P
from .hook import socket_path

log = logging.getLogger("macropadd.agents")

# Cyrillic -> ASCII for the OLED's terminal font.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if ch.isupper() and t else t)
        elif ord(ch) < 128:
            out.append(ch)
        elif ch in "—–":
            out.append("-")
        else:
            out.append("?")
    return "".join(out)

IDLE, WORK, TOOL, WAIT, ERROR, DONE = "idle", "work", "tool", "wait", "error", "done"
PRIORITY = {ERROR: 1, WAIT: 2, TOOL: 3, WORK: 5, DONE: 6, IDLE: 7}
ACTIVE = (WORK, TOOL)
PROVIDER_LETTER = {"claude": "C", "codex": "X"}

SLOTS = 7                          # keys 0-6; 7 push-to-talk (double tap: Enter), 8 the usage page
TOKENS_EVERY_S = 5.0               # transcript titles and token totals refresh
DONE_VISIBLE_S = 20 * 60.0         # SidePulse COMPLETED_VISIBLE_SECONDS
STALE_S = 3600.0                   # active modes with no event for an hour
POST_TOOL_WORKING_S = 120.0        # "working" after a tool with no follow-up: the turn ended unseen

NAME_MAX, TITLE_MAX, DETAIL_MAX = 12, 40, 200   # the pad scrolls the detail with the knob
MODE_SHORT = {"default": "ask", "acceptEdits": "edits", "plan": "plan", "auto": "auto",
              "bypassPermissions": "bypass", "dontAsk": "noask"}


# ------------------------------------------------------------ event -> mode
def tool_summary(tool: str, tool_input: object) -> str:
    """'Bash: npm test', 'Edit: app.py', 'WebFetch: example.com'."""
    if not isinstance(tool_input, dict):
        return tool
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            if key in ("file_path", "path"):
                val = Path(val).name or val
            return f"{tool}: {' '.join(val.split())}"
    return tool


def tool_failed(response: object) -> bool:
    if isinstance(response, dict):
        return (response.get("interrupted") is True or response.get("success") is False
                or response.get("exit_code") not in (None, 0))
    if isinstance(response, str):
        text = response.lower()
        return "exit code: 1" in text or "traceback" in text
    return False


_ASK_PREFIXES = ("which ", "what ", "where ", "when ", "who ", "why ", "how ", "can you ", "could you ")
_ASK_ALWAYS = ("please confirm", "please choose", "choose ", "need me to ", "want me to ",
               "should i ", "should we ", "do you want me to ")
_CASUAL = ("anything else", "any other", "all good", "need anything else", "want anything else",
           "anything you want", "anything you'd like")


def question_line(message: object) -> str | None:
    """The line where the assistant's final message asks the user something, if any."""
    if not isinstance(message, str):
        return None
    text = re.sub(r"```.*?```", "", message, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", "", text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in reversed(lines[-8:]):
        low = line.lower().lstrip("*>-# ")
        if low.startswith(("cogitated ", "recap:", "※ recap:")):
            continue
        if line.endswith(":") or low.startswith(_CASUAL):
            continue
        if re.search(r"(?:^|[.!?]\s+)(?:want me to|need me to|should i|should we|do you want me to)\b", low):
            return line
        if line.endswith("?") and low.startswith(_ASK_PREFIXES + _ASK_ALWAYS):
            return line
        if low.startswith(_ASK_ALWAYS):
            return line
    return None


_DONE_PHRASES = ("turn complete", "task complete", "completed successfully", "work complete")
_INPUT_PHRASES = ("waiting for your input", "waiting for input", "needs your input", "needs input",
                  "permission", "approval", "confirm")
_INPUT_TYPES = ("permission_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog")


def mode_for_event(event: dict) -> str | None:
    name = event.get("hook_event_name")
    if name == "Interrupt":
        return IDLE                    # inactive, but not finished
    if name in ("PostToolUseFailure", "PermissionDenied", "StopFailure"):
        return ERROR
    if name == "PermissionRequest":
        return WAIT
    if name == "Notification":
        kind = str(event.get("notification_type", "")).lower()
        text = f"{kind} {event.get('message', '')}".lower()
        if kind == "idle_prompt":
            return DONE                # "Claude is waiting for your input": 60 s after a finished turn
        if kind == "agent_completed" or any(p in text for p in _DONE_PHRASES):
            return DONE
        if kind in _INPUT_TYPES or any(p in text for p in _INPUT_PHRASES):
            return WAIT
        return WORK
    if name == "PreToolUse":
        return TOOL
    if name == "PostToolUse":
        return ERROR if tool_failed(event.get("tool_response")) else WORK
    if name in ("UserPromptSubmit", "PreCompact", "PostCompact", "SubagentStart"):
        return WORK
    if name == "SubagentStop":
        return None                    # the session's own events say what comes next
    if name == "Stop":
        return WAIT if question_line(event.get("last_assistant_message")) else DONE
    if name == "SessionStart":
        return IDLE
    return None


def detail_for_event(event: dict, mode: str) -> str:
    name = event.get("hook_event_name")
    tool = str(event.get("tool_name") or "")
    if name in ("PreToolUse", "PermissionRequest", "PostToolUseFailure", "PermissionDenied"):
        text = tool_summary(tool, event.get("tool_input"))
        err = event.get("error")
        return f"{text} - {err}" if name == "PostToolUseFailure" and isinstance(err, str) else text
    if name == "Stop":
        msg = event.get("last_assistant_message")
        asked = question_line(msg)
        if asked:
            return asked
        # the reply itself, markdown bullets and emphasis dropped: readable on the pad
        text = re.sub(r"```.*?```", " ", str(msg or ""), flags=re.DOTALL)
        return " ".join(re.sub(r"^\s*(?:[-*>#]+\s*)+", "", l).replace("**", "").replace("`", "")
                        for l in text.splitlines() if l.strip())
    if name == "Notification":
        return str(event.get("message") or "")
    if name == "StopFailure":
        return str(event.get("error") or event.get("message") or "turn failed")
    return ""


_CODEX_INTERNAL = ("generate 0 to 3 hyperpersonalized suggestions",
                   "you are an expert at upholding safety and compliance standards")


def is_internal(provider: str, event: dict) -> bool:
    """Codex's own background prompts (suggestions, safety review) are not your agents."""
    if provider != "codex":
        return False
    text = " ".join(str(event.get(k) or "") for k in ("prompt", "message", "last_assistant_message")).lower()
    return any(p in text for p in _CODEX_INTERNAL)


def session_started(transcript: object) -> float | None:
    """When the session began: the first top-level `timestamp` in its transcript
    (Codex: the session_meta line; Claude: the first prompt after a few
    metadata lines). Survives daemon restarts, unlike first-seen times."""
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        with open(transcript, encoding="utf-8") as fh:
            for _, line in zip(range(40), fh):
                try:
                    stamp = json.loads(line).get("timestamp")
                except (ValueError, AttributeError):
                    continue
                if isinstance(stamp, str):
                    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError):
        pass
    return None


class Transcript:
    """A session's title and token totals, read from its transcript a slice at a time.

    Title: Claude writes `ai-title` lines (and `custom-title` after /rename,
    which wins); Codex keeps `thread_name` per session in
    $CODEX_HOME/session_index.jsonl, re-read when that file changes.
    Tokens: Claude: every assistant message carries `usage`; a message is written
    once per streamed part with the same id, so totals are per id. "In"
    counts what the model read: input + cache writes + cache reads.
    Codex: `token_count` events carry the running total; the last wins.
    Only appended bytes are read, and only lines that can hold usage are
    parsed: transcripts run to megabytes.
    """

    def __init__(self, path: str, provider: str, session: str = ""):
        self.path = path
        self.provider = provider
        self.session = session
        self.offset = 0
        self.input = self.output = 0
        self._per_message: dict[str, tuple[int, int]] = {}
        self._ai_title = self._custom_title = ""
        self._index_mtime: float | None = None
        self.limits: dict[str, tuple[float, float]] = {}    # Codex: {"5h"|"wk": (percent, resets_at)}

    @property
    def title(self) -> str:
        return self._custom_title or self._ai_title

    def refresh(self) -> bool:
        """Read what was appended. True when the title or the totals changed."""
        before = (self.input, self.output, self.title)
        if self.provider == "codex":
            self._codex_title()
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            chunk = b""
        end = chunk.rfind(b"\n") + 1            # a half-written last line waits for the next pass
        if end:
            self.offset += end
            markers = ((b'"token_count"',) if self.provider == "codex"
                       else (b'"usage"', b'"ai-title"', b'"custom-title"'))
            for line in chunk[:end].splitlines():
                if any(m in line for m in markers):
                    self._line(line)
        return (self.input, self.output, self.title) != before

    def _codex_title(self) -> None:
        index = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "session_index.jsonl"
        try:
            mtime = index.stat().st_mtime
            if mtime == self._index_mtime:
                return
            self._index_mtime = mtime
            needle = self.session.encode()
            with index.open("rb") as fh:
                for line in fh:
                    if needle and needle in line:
                        try:
                            self._ai_title = str(json.loads(line).get("thread_name") or self._ai_title)
                        except ValueError:
                            pass
        except OSError:
            pass

    def _line(self, line: bytes) -> None:
        try:
            row = json.loads(line)
        except ValueError:
            return
        if self.provider == "codex":
            payload = row.get("payload") or {}
            total = ((payload.get("info") or {}).get("total_token_usage")
                     if payload.get("type") == "token_count" else None)
            if isinstance(total, dict):
                self.input = int(total.get("input_tokens") or 0)
                self.output = int(total.get("output_tokens") or 0) + int(total.get("reasoning_output_tokens") or 0)
            limits = payload.get("rate_limits") if payload.get("type") == "token_count" else None
            if isinstance(limits, dict):
                self.limits = codex_limits(limits) or self.limits
            return
        if row.get("type") == "ai-title":
            self._ai_title = str(row.get("aiTitle") or self._ai_title)
            return
        if row.get("type") == "custom-title":
            self._custom_title = str(row.get("customTitle") or self._custom_title)
            return
        msg = row.get("message") if row.get("type") == "assistant" else None
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            return
        new = (sum(int(usage.get(k) or 0) for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
               int(usage.get("output_tokens") or 0))
        old = self._per_message.get(str(msg.get("id")), (0, 0))
        self._per_message[str(msg.get("id"))] = new
        self.input += new[0] - old[0]
        self.output += new[1] - old[1]


WINDOWS = {300: "5h", 10080: "wk"}            # Codex window_minutes -> our name


def codex_limits(raw: dict) -> dict[str, tuple[float, float]]:
    """Codex `rate_limits` (primary 5 h, secondary weekly) -> {"5h": (%, resets_at)}."""
    out = {}
    for key in ("primary", "secondary"):
        w = raw.get(key)
        if isinstance(w, dict) and w.get("window_minutes") in WINDOWS:
            out[WINDOWS[w["window_minutes"]]] = (float(w.get("used_percent") or 0), float(w.get("resets_at") or 0))
    return out


def claude_limits(raw: dict) -> dict[str, tuple[float, float]]:
    """Claude's status line `rate_limits` (subscribers only) -> {"5h": (%, resets_at)}."""
    out = {}
    for key, name in (("five_hour", "5h"), ("seven_day", "wk")):
        w = raw.get(key)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            out[name] = (float(w["used_percentage"]), float(w.get("resets_at") or 0))
    return out


def project_name(cwd: str | None) -> str:
    if not cwd:
        return "?"
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate.name or str(candidate)
    return path.name or cwd


def clean(text: str, limit: int) -> str:
    """ASCII for the OLED font, one line, at most `limit` characters."""
    return " ".join(translit(text or "").split())[:limit]


def _signature(event: dict) -> str | None:
    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        return f"{event.get('tool_name') or ''}\0{tool_input['command']}"
    return None


# ------------------------------------------------------------------ tracker
@dataclass
class Permission:
    id: int
    agent_key: str
    text: str
    created: float
    decision: str | None = None        # allow | deny | ask
    done: threading.Event = field(default_factory=threading.Event)
    rules: list[dict] = field(default_factory=list)      # what "don't ask again" would save
    updates: list[dict] = field(default_factory=list)    # sent back with an "always" / "auto" answer
    auto_ok: bool = False                                 # "Yes, and switch to auto mode" possible


AUTO_MODE = {"type": "setMode", "mode": "auto", "destination": "session"}


def can_switch_to_auto(provider: str, event: dict) -> bool:
    """Claude only, and only from a mode that still asks."""
    return provider == "claude" and event.get("permission_mode", "default") in ("default", "acceptEdits", "plan")


def always_rules(provider: str, event: dict) -> list[dict]:
    """The permission updates Claude suggests for "Yes, and don't ask again":
    allow rules and extra directories, never a mode change. Codex has no
    such thing (its hooks may only allow or deny)."""
    if provider != "claude":
        return []
    out = []
    for s in event.get("permission_suggestions") or ():
        if not isinstance(s, dict):
            continue
        if (s.get("type") == "addRules" and s.get("behavior") == "allow") or s.get("type") == "addDirectories":
            out.append(s)
    return out


@dataclass
class Agent:
    key: str
    provider: str
    session: str
    cwd: str
    name: str
    mode: str = IDLE
    event: str = ""
    updated: float = 0.0               # wall time of the last event
    since: float = 0.0                 # wall time the current mode began
    started: float = 0.0               # wall time the session began
    transcript: Transcript | None = None

    @property
    def title(self) -> str:
        """The session's title (Claude's ai-title or /rename, Codex's thread name), or ''."""
        return self.transcript.title if self.transcript is not None else ""
    perm_mode: str = ""                # Claude's permission mode, as of the last event
    detail: str = ""
    slot: int | None = None
    signatures: set[str] = field(default_factory=set)    # commands asked about in the terminal
    permissions: list[Permission] = field(default_factory=list)   # waiting for the pad


class AgentTracker:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.agents: dict[str, Agent] = {}
        self._next_permission = 1
        self.dirty = False
        self.log: deque[dict] = deque(maxlen=200)    # `macropadd agents log`: what came in, what it did
        self._first_seen: dict[str, float] = {}       # SessionStart of sessions not shown yet
        self.usage: dict[str, dict[str, tuple[float, float]]] = {}   # "C"/"X" -> rate-limit windows

    # ---------------------------------------------------------- ingest
    def ingest(self, provider: str, event: dict) -> Agent | None:
        """Apply one hook event. Returns the agent it changed, if any."""
        with self._lock:
            now = self._clock()
            changed = self._ingest(provider, event, now)
            self._record(provider, event, now, changed)
            return changed

    def _record(self, provider: str, event: dict, now: float, changed: Agent | None) -> None:
        key = f"{provider}:{event.get('session_id') or '?'}"
        agent = self.agents.get(key)
        extra = event.get("notification_type") or event.get("tool_name") or ""
        self.log.append({"t": now, "provider": provider, "session": str(event.get("session_id") or "?"),
                         "event": str(event.get("hook_event_name")), "extra": str(extra),
                         "mode": agent.mode if agent else "-", "applied": changed is not None})

    def _ingest(self, provider: str, event: dict, now: float) -> Agent | None:
        name = event.get("hook_event_name")
        if not isinstance(name, str) or is_internal(provider, event):
            return None
        key = f"{provider}:{event.get('session_id') or '?'}"
        agent = self.agents.get(key)
        if name == "SessionEnd":
            if agent is not None:
                self._remove(agent)
            return None
        mode = mode_for_event(event)
        if mode is None:
            return None
        if agent is None:
            if name == "SessionStart" and len(self._first_seen) < 64:
                self._first_seen.setdefault(key, now)
            if mode == IDLE:
                return None            # a fresh or interrupted session shows nothing
            if name == "Notification":
                # Claude Code sometimes notifies under a conversation's previous
                # session id; on its own that would be a ghost row stuck amber.
                return None
            cwd = str(event.get("cwd") or "")
            agent = Agent(key, provider, str(event.get("session_id") or "?"), cwd, project_name(cwd))
            agent.started = (session_started(event.get("transcript_path"))
                             or self._first_seen.pop(key, now))
            self.agents[key] = agent
        transcript = event.get("transcript_path")
        if isinstance(transcript, str) and transcript and (agent.transcript is None
                                                           or agent.transcript.path != transcript):
            agent.transcript = Transcript(transcript, provider, agent.session)
        # SidePulse's guards: a late Notification never undoes "done" (nor does work
        # the agent does on its own after the turn, like titling the session), and
        # while the terminal shows a permission prompt only the answer moves it on.
        if agent.mode == DONE and name in ("Notification", "SubagentStart", "PreCompact", "PostCompact"):
            return None
        sig = _signature(event)
        if name == "PermissionRequest" and sig:
            agent.signatures.add(sig)
        elif name == "PostToolUse" and sig:
            agent.signatures.discard(sig)
        elif name in ("Stop", "Interrupt", "UserPromptSubmit"):
            agent.signatures.clear()
        if (agent.mode == WAIT and agent.event == "PermissionRequest" and name != "PermissionRequest"
                and (agent.signatures or agent.permissions)):
            agent.updated = now
            return None
        if isinstance(event.get("permission_mode"), str):
            agent.perm_mode = event["permission_mode"]
        if event.get("cwd") and not agent.cwd:
            agent.cwd = str(event["cwd"])
            agent.name = project_name(agent.cwd)
        if mode != agent.mode:
            agent.since = now
        agent.mode = mode
        agent.event = name
        agent.updated = now
        detail = detail_for_event(event, mode)
        if detail or mode in (WORK, IDLE):
            agent.detail = clean(detail, DETAIL_MAX)
        if mode == IDLE:
            self._remove(agent)
            return None
        if agent.slot is None:
            agent.slot = self._free_slot()
        self.dirty = True
        return agent

    def _free_slot(self) -> int | None:
        used = {a.slot for a in self.agents.values()}
        return next((i for i in range(SLOTS) if i not in used), None)

    def _remove(self, agent: Agent) -> None:
        self.agents.pop(agent.key, None)
        for perm in agent.permissions:
            perm.decision = None
            perm.done.set()
        self.dirty = True
        # A session waiting for a key gets the freed one.
        if agent.slot is not None:
            waiting = sorted((a for a in self.agents.values() if a.slot is None),
                             key=lambda a: (PRIORITY[a.mode], -a.updated))
            if waiting:
                waiting[0].slot = agent.slot

    # ---------------------------------------------------------- expiry
    def expire(self) -> bool:
        with self._lock:
            now = self._clock()
            changed = False
            for agent in list(self.agents.values()):
                if agent.permissions:
                    continue
                age = now - agent.updated
                if agent.mode == WORK and agent.event == "PostToolUse" and age > POST_TOOL_WORKING_S:
                    agent.mode, agent.since = DONE, now
                    self.dirty = changed = True
                elif (agent.mode == DONE and age > DONE_VISIBLE_S) or (agent.mode != DONE and age > STALE_S):
                    self._remove(agent)
                    changed = True
            return changed

    def dismiss(self, slot: int) -> bool:
        """Forget one session (any state but a held permission request), or
        with slot -1 all finished ones. A session whose agent went away
        without SessionEnd (or changed its id) would otherwise linger an hour."""
        with self._lock:
            gone = [a for a in self.agents.values() if not a.permissions
                    and (a.slot == slot if slot >= 0 else a.mode == DONE)]
            for agent in gone:
                self._remove(agent)
            return bool(gone)

    # ----------------------------------------------------- permissions
    def ask(self, provider: str, event: dict) -> Permission | None:
        """Register a PermissionRequest the pad may answer."""
        with self._lock:
            now = self._clock()
            changed = self._ingest(provider, event, now)
            self._record(provider, event, now, changed)
            agent = changed or self.agents.get(f"{provider}:{event.get('session_id') or '?'}")
            if agent is None:
                return None
            perm = Permission(self._next_permission, agent.key,
                              clean(tool_summary(str(event.get("tool_name") or "tool"), event.get("tool_input")),
                                    DETAIL_MAX), now, rules=always_rules(provider, event),
                              auto_ok=can_switch_to_auto(provider, event))
            self._next_permission += 1
            agent.permissions.append(perm)
            self.dirty = True
            return perm

    def decide(self, perm_id: int, decision: str) -> bool:
        with self._lock:
            for agent in self.agents.values():
                for perm in agent.permissions:
                    if perm.id == perm_id:
                        if decision == "always":
                            perm.updates = perm.rules        # nothing to save: a plain allow
                            decision = "allow"
                        elif decision == "auto":
                            perm.updates = [AUTO_MODE] if perm.auto_ok else []
                            decision = "allow"
                        perm.decision = decision if decision in ("allow", "deny") else None
                        self._close(agent, perm)
                        if perm.decision == "allow":
                            agent.mode, agent.event, agent.since = TOOL, "PreToolUse", self._clock()
                            agent.signatures.clear()
                        elif perm.decision == "deny":
                            agent.mode, agent.event, agent.since = WORK, "PermissionDenied", self._clock()
                            agent.signatures.clear()
                        return True
            return False

    def withdraw(self, perm: Permission) -> None:
        """The hook stopped waiting (timeout, agent went away)."""
        with self._lock:
            agent = self.agents.get(perm.agent_key)
            if agent is not None and perm in agent.permissions:
                self._close(agent, perm)

    def _close(self, agent: Agent, perm: Permission) -> None:
        agent.permissions.remove(perm)
        perm.done.set()
        self.dirty = True

    def pending(self) -> int:
        with self._lock:
            return sum(len(a.permissions) for a in self.agents.values())

    # -------------------------------------------------------- snapshot
    def visible(self) -> list[Agent]:
        return sorted((a for a in self.agents.values() if a.slot is not None), key=lambda a: a.slot)

    def refresh_transcripts(self) -> bool:
        """New titles, token totals and Codex limits for every session (file IO outside the lock)."""
        with self._lock:
            counters = [a.transcript for a in self.agents.values() if a.transcript is not None]
        changed = False
        for counter in counters:
            changed = counter.refresh() or changed
        for counter in counters:
            if counter.provider == "codex" and counter.limits:
                changed = self.set_usage("X", counter.limits) or changed
        if changed:
            with self._lock:
                self.dirty = True
        return changed

    def set_usage(self, letter: str, windows: dict[str, tuple[float, float]]) -> bool:
        """Account-wide limits for one agent ("C" Claude, "X" Codex). True when changed."""
        with self._lock:
            if not windows or self.usage.get(letter) == windows:
                return False
            self.usage[letter] = dict(windows)
            self.dirty = True
            return True

    def usage_message(self) -> dict:
        """{"C": [pct 5h, s to reset, pct week, s to reset]}; -1 where unknown."""
        now = self._clock()
        out = {}
        for letter, w in self.usage.items():
            row = []
            for name in ("5h", "wk"):
                pct, resets = w.get(name, (-1.0, 0.0))
                row += [int(round(pct)), int(max(0.0, resets - now)) if resets else -1]
            out[letter] = row
        return out

    def aggregate(self) -> str:
        with self._lock:
            modes = [a.mode for a in self.agents.values()]
        return min(modes, key=PRIORITY.__getitem__) if modes else IDLE

    def state_message(self) -> dict:
        with self._lock:
            now = self._clock()
            rows = []
            for a in self.visible():
                row = {"k": a.slot, "p": PROVIDER_LETTER.get(a.provider, "?"), "n": clean(a.name, NAME_MAX),
                       "m": a.mode, "s": int(max(0.0, now - a.since)),
                       "t": clean(a.title, TITLE_MAX),
                       "st": int(max(0.0, now - a.started))}
                if a.mode in (TOOL, ERROR):
                    row["d"] = a.detail               # the command or the error; not prompts or replies
                if a.transcript is not None and (a.transcript.input or a.transcript.output):
                    row["tk"] = [a.transcript.input, a.transcript.output]
                if a.perm_mode:
                    row["pm"] = MODE_SHORT.get(a.perm_mode, a.perm_mode[:6])
                if a.permissions:
                    perm = a.permissions[0]
                    row.update(m=WAIT, q=perm.id, d=perm.text)
                    if perm.rules:
                        row["a"] = 1                  # the pad offers "hold YES: don't ask again"
                    if perm.auto_ok:
                        row["u"] = 1                  # ... and "hold longer: auto mode"
                rows.append(row)
            more = sum(1 for a in self.agents.values() if a.slot is None)
            self.dirty = False
            return P.agents_state(rows, more, self.usage_message())

    def rows(self) -> list[dict]:
        """For `macropadd agents ls`: the full picture, newest first."""
        with self._lock:
            now = self._clock()
            return [{"slot": a.slot, "provider": a.provider, "session": a.session, "name": a.name,
                     "cwd": a.cwd, "mode": a.mode, "event": a.event, "age_s": int(now - a.updated),
                     "title": a.title,
                     "detail": a.detail, "pending": [p.text for p in a.permissions],
                     "tokens": [a.transcript.input, a.transcript.output] if a.transcript else None}
                    for a in sorted(self.agents.values(), key=lambda a: -a.updated)]


# --------------------------------------------------------------- keep awake
class KeepAwake:
    """Hold off system sleep while agents work, like SidePulse's caffeinate.

    Active modes hold; done / wait / error keep holding for a grace period
    (so you can come back to the answer); idle releases.
    """

    GRACE_S = 300.0

    def __init__(self, enabled: bool = True, command: list[str] | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.enabled = enabled
        self.command = command or self.default_command()
        self._popen = popen
        self.proc: subprocess.Popen | None = None
        self._grace_until: float | None = None
        self._last: str | None = None

    @staticmethod
    def default_command() -> list[str] | None:
        if sys.platform == "darwin":
            return ["/usr/bin/caffeinate", "-ims"]
        if shutil.which("systemd-inhibit"):
            return ["systemd-inhibit", "--what=sleep", "--who=macropadd",
                    "--why=AI agents are working", "--mode=block", "sleep", "infinity"]
        return None

    def should_hold(self, mode: str, now: float) -> bool:
        if mode in ACTIVE:
            self._grace_until = None
            hold = True
        elif mode in (DONE, WAIT, ERROR):
            if mode != self._last or self._grace_until is None:
                self._grace_until = now + self.GRACE_S
            hold = now < self._grace_until
        else:
            hold = self._grace_until is not None and now < self._grace_until
        self._last = mode
        return hold

    def update(self, mode: str, now: float) -> None:
        if self.enabled and self.command and self.should_hold(mode, now):
            if self.proc is None or self.proc.poll() is not None:
                try:
                    self.proc = self._popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
                    log.info("keep awake: holding (%s)", self.command[0])
                except OSError as exc:
                    log.warning("keep awake: %s", exc)
                    self.enabled = False
        else:
            self.release()

    def release(self) -> None:
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
            self.proc = None
            log.info("keep awake: released")


# ------------------------------------------------------------ socket server
class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(16 * 1024 * 1024)
        try:
            msg = json.loads(line or b"{}")
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        reply = self.server.service.on_socket_message(msg)   # type: ignore[attr-defined]
        if reply is not None:
            try:
                self.wfile.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            except OSError:
                pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class AgentServer:
    """Unix socket the hooks talk to; one thread per connection."""

    def __init__(self, service: "AgentService", path: Path | None = None):
        self.path = (path or socket_path()).expanduser()
        self._service = service
        self._server: _Server | None = None

    def start(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if _socket_alive(self.path):
                log.warning("agents: %s is served by another process; AGENTS disabled here", self.path)
                return False
            self.path.unlink()
        self._server = _Server(str(self.path), _Handler)
        self._server.service = self._service   # type: ignore[attr-defined]
        os.chmod(self.path, 0o600)
        threading.Thread(target=self._server.serve_forever, daemon=True, name="agents-socket").start()
        log.info("agents: listening on %s", self.path)
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            try:
                self.path.unlink()
            except OSError:
                pass


def _socket_alive(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.3)
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def query(path: Path | None = None, what: str = "agents") -> list[dict] | None:
    """The running daemon's agents (or its event log), or None when it is not reachable."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(2.0)
        s.connect(str((path or socket_path()).expanduser()))
        s.sendall(b'{"query":true}\n')
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf or b"{}").get(what)
    except (OSError, ValueError):
        return None
    finally:
        s.close()


# ------------------------------------------------------------------ service
class AgentService:
    """Protocol side: `agents_state` to the pad, `agent_decide` / `agent_dismiss` from it."""

    def __init__(self, tracker: AgentTracker | None = None, approve_on_pad: bool = True,
                 approve_wait_s: float = 30.0, device_online: Callable[[], bool] = lambda: True,
                 keep_awake: KeepAwake | None = None):
        self.tracker = tracker or AgentTracker()
        self.approve_on_pad = approve_on_pad
        self.approve_wait_s = approve_wait_s
        self._device_online = device_online
        self.keep_awake = keep_awake
        self._sent_at = 0.0
        self._tokens_at = 0.0

    # socket threads
    def on_socket_message(self, msg: dict) -> dict | None:
        if msg.get("query"):
            return {"agents": self.tracker.rows(), "log": list(self.tracker.log),
                    "usage": self.tracker.usage_message()}
        provider = str(msg.get("provider") or "claude")
        status = msg.get("status")
        if isinstance(status, dict):              # Claude's status line: subscription limits
            limits = status.get("rate_limits")
            if isinstance(limits, dict):
                self.tracker.set_usage(PROVIDER_LETTER.get(provider, "?"), claude_limits(limits))
            return None
        event = msg.get("event")
        if not isinstance(event, dict):
            return None
        if msg.get("wait") and event.get("hook_event_name") == "PermissionRequest":
            if not (self.approve_on_pad and self._device_online()):
                self.tracker.ingest(provider, event)
                return {"decision": None}
            perm = self.tracker.ask(provider, event)
            if perm is None:
                return {"decision": None}
            log.info("agents: permission #%d %s, waiting %.0fs for the pad", perm.id, perm.text, self.approve_wait_s)
            if not perm.done.wait(self.approve_wait_s):
                self.tracker.withdraw(perm)
                log.info("agents: permission #%d timed out, back to the terminal", perm.id)
            return {"decision": perm.decision, "permissions": perm.updates}
        self.tracker.ingest(provider, event)
        return None

    # serial loop
    def tick(self, now: float) -> list[dict]:
        """Called every loop pass: expiry, keep-awake, and a state push when something changed."""
        self.tracker.expire()
        if now - self._tokens_at >= TOKENS_EVERY_S:
            self._tokens_at = now
            self.tracker.refresh_transcripts()
        if self.keep_awake is not None:
            self.keep_awake.update(self.tracker.aggregate(), now)
        if self.tracker.dirty and now - self._sent_at >= 0.2:
            self._sent_at = now
            return [self.tracker.state_message()]
        return []

    def state_messages(self) -> list[dict]:
        return [self.tracker.state_message()]

    def handle_decide(self, msg: dict, now: float) -> list[dict]:
        try:
            perm_id = int(msg.get("id"))
        except (TypeError, ValueError):
            return [P.error("agent_decide needs an id")]
        decision = str(msg.get("decision", ""))
        if not self.tracker.decide(perm_id, decision):
            return [P.overlay("REQUEST GONE")]
        log.info("agents: permission #%d -> %s", perm_id, decision)
        return []

    def handle_dismiss(self, msg: dict, now: float) -> list[dict]:
        try:
            slot = int(msg.get("k", -1))
        except (TypeError, ValueError):
            slot = -1
        self.tracker.dismiss(slot)
        return []

    def handlers(self) -> dict:
        return {"agent_decide": self.handle_decide, "agent_dismiss": self.handle_dismiss}

    def close(self) -> None:
        if self.keep_awake is not None:
            self.keep_awake.release()
