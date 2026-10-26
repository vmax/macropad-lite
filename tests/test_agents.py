import json
import threading

from macropadd import agents as A
from macropadd import hook as H


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def ev(name, session="s1", **kw):
    return {"hook_event_name": name, "session_id": session, "cwd": "/home/u/proj", **kw}


def test_mode_mapping_follows_sidepulse():
    assert A.mode_for_event(ev("UserPromptSubmit")) == A.WORK
    assert A.mode_for_event(ev("PreToolUse", tool_name="Bash")) == A.TOOL
    assert A.mode_for_event(ev("PostToolUse", tool_response={"exit_code": 0})) == A.WORK
    assert A.mode_for_event(ev("PostToolUse", tool_response={"interrupted": True})) == A.ERROR
    assert A.mode_for_event(ev("PostToolUseFailure")) == A.ERROR
    assert A.mode_for_event(ev("PermissionRequest")) == A.WAIT
    assert A.mode_for_event(ev("Notification", notification_type="permission_prompt", message="x")) == A.WAIT
    assert A.mode_for_event(ev("Notification", message="Claude is waiting for your input")) == A.WAIT
    assert A.mode_for_event(ev("Notification", notification_type="agent_completed")) == A.DONE
    assert A.mode_for_event(ev("Stop", last_assistant_message="All tests pass.")) == A.DONE
    assert A.mode_for_event(ev("Stop", last_assistant_message="Done.\nShould I push it?")) == A.WAIT
    assert A.mode_for_event(ev("Stop", last_assistant_message="Fixed.\nAnything else?")) == A.DONE
    assert A.mode_for_event(ev("Notification", notification_type="idle_prompt",
                                message="Claude is waiting for your input")) == A.DONE
    assert A.mode_for_event(ev("SubagentStop")) is None
    assert A.mode_for_event(ev("Interrupt")) == A.IDLE
    assert A.mode_for_event(ev("SessionStart")) == A.IDLE
    assert A.mode_for_event(ev("Whatever")) is None


def test_question_detection_skips_code_and_casual_closers():
    assert A.question_line("Which region do you want?") == "Which region do you want?"
    assert A.question_line("Want me to open a PR?") == "Want me to open a PR?"
    assert A.question_line("```\nshould i be here?\n```\nDone.") is None
    assert A.question_line("Here is how it works:") is None
    assert A.question_line("Is it fine?") is None        # not one of the asking forms


def test_tool_summary():
    assert A.tool_summary("Bash", {"command": "npm  test"}) == "Bash: npm test"
    assert A.tool_summary("Edit", {"file_path": "/a/b/app.py"}) == "Edit: app.py"
    assert A.tool_summary("Task", None) == "Task"


def test_session_lifecycle_and_slots(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "sub").mkdir()
    clock = Clock()
    t = A.AgentTracker(clock)
    assert t.ingest("claude", ev("SessionStart")) is None and not t.agents   # idle shows nothing
    a = t.ingest("claude", ev("UserPromptSubmit", prompt="Fix the  flaky test", cwd=str(tmp_path / "sub")))
    assert a.mode == A.WORK and a.slot == 0 and a.name == tmp_path.name and a.title == ""
    b = t.ingest("codex", ev("UserPromptSubmit", session="s2"))
    assert b.slot == 1 and b.key == "codex:s2"
    t.ingest("claude", ev("PreToolUse", tool_name="Bash", tool_input={"command": "pytest"}))
    assert a.mode == A.TOOL and a.detail == "Bash: pytest"
    clock.t += 5
    t.ingest("claude", ev("Stop", last_assistant_message="All green."))
    assert a.mode == A.DONE and a.since == clock.t and a.detail == "All green."
    t.ingest("claude", ev("Notification", message="Claude is waiting for your input"))
    assert a.mode == A.DONE                                   # late notification ignored
    t.ingest("claude", ev("SubagentStart"))
    t.ingest("claude", ev("SubagentStop"))
    assert a.mode == A.DONE                                   # post-turn work (titles) is not work
    msg = t.state_message()
    assert msg["type"] == "agents_state" and [r["k"] for r in msg["agents"]] == [0, 1]
    assert msg["agents"][0]["p"] == "C" and msg["agents"][1]["p"] == "X" and msg["agents"][0]["m"] == "done"
    t.ingest("claude", ev("SessionEnd"))
    assert list(t.agents) == ["codex:s2"]
    c = t.ingest("claude", ev("UserPromptSubmit", session="s3"))
    assert c.slot == 0                                        # freed key reused


def test_expiry_rules():
    clock = Clock()
    t = A.AgentTracker(clock)
    a = t.ingest("claude", ev("PostToolUse", tool_response={}))
    clock.t += A.POST_TOOL_WORKING_S + 1
    assert t.expire() and a.mode == A.DONE                    # tool finished, then silence
    clock.t += A.DONE_VISIBLE_S + 1
    t.expire()
    assert not t.agents
    t.ingest("codex", ev("UserPromptSubmit"))
    clock.t += A.STALE_S + 1
    t.expire()
    assert not t.agents


def test_permission_signatures_hold_wait_until_answered():
    t = A.AgentTracker(Clock())
    req = ev("PermissionRequest", tool_name="Bash", tool_input={"command": "rm -rf x"})
    a = t.ingest("claude", req)
    assert a.mode == A.WAIT
    t.ingest("claude", ev("Notification", notification_type="idle_prompt", message="hi"))
    assert a.mode == A.WAIT
    t.ingest("claude", ev("PostToolUse", tool_name="Bash", tool_input={"command": "rm -rf x"}, tool_response={}))
    assert a.mode == A.WORK


def test_dismiss_one_any_state_or_all_finished():
    t = A.AgentTracker(Clock())
    t.ingest("claude", ev("Stop", last_assistant_message="ok"))
    t.ingest("claude", ev("UserPromptSubmit", session="s2"))
    t.ingest("claude", ev("UserPromptSubmit", session="s3"))
    assert t.dismiss(-1) and list(t.agents) == ["claude:s2", "claude:s3"]
    assert t.dismiss(1) and list(t.agents) == ["claude:s3"]          # a stuck one, by key
    t.ask("claude", ev("PermissionRequest", session="s3"))
    assert not t.dismiss(2)                                           # never a held request


def test_overflow_sessions_wait_for_a_key():
    t = A.AgentTracker(Clock())
    for i in range(A.SLOTS + 2):
        t.ingest("claude", ev("UserPromptSubmit", session="s%d" % i))
    msg = t.state_message()
    assert len(msg["agents"]) == A.SLOTS and msg["more"] == 2
    t.ingest("claude", ev("SessionEnd", session="s3"))
    assert any(a.slot == 3 for a in t.agents.values() if a.session in ("s%d" % A.SLOTS, "s%d" % (A.SLOTS + 1)))


def test_keep_awake_holds_while_working_then_grace():
    started = []

    class Proc:
        def __init__(self, *a, **k):
            started.append(a[0])
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

    k = A.KeepAwake(True, ["inhibit"], popen=Proc)
    k.update(A.WORK, 0.0)
    assert k.proc is not None and started == [["inhibit"]]
    k.update(A.DONE, 10.0)
    assert k.proc is not None                                # grace
    k.update(A.DONE, 10.0 + A.KeepAwake.GRACE_S + 1)
    assert k.proc is None
    k.update(A.IDLE, 1000.0)
    assert k.proc is None and len(started) == 1


def socket_service(tmp_path, monkeypatch, **kw):
    path = tmp_path / "agents.sock"
    monkeypatch.setenv("MACROPADD_AGENTS_SOCKET", str(path))
    svc = A.AgentService(**kw)
    server = A.AgentServer(svc, path)
    assert server.start()
    return svc, server, path


def test_hook_events_reach_the_tracker_over_the_socket(tmp_path, monkeypatch):
    svc, server, path = socket_service(tmp_path, monkeypatch)
    try:
        assert H.forward("claude", ev("UserPromptSubmit", prompt="hi"), path) == (None, [])
        for _ in range(100):
            if svc.tracker.agents:
                break
            threading.Event().wait(0.01)
        assert list(svc.tracker.agents) == ["claude:s1"]
        rows = A.query(path)
        assert rows[0]["mode"] == "work" and rows[0]["title"] == ""
        assert svc.tick(10.0)[0]["type"] == "agents_state"
    finally:
        server.stop()
    assert not path.exists()


def test_permission_answered_from_the_pad(tmp_path, monkeypatch):
    svc, server, path = socket_service(tmp_path, monkeypatch, approve_wait_s=5.0)
    try:
        result = {}
        req = ev("PermissionRequest", tool_name="Bash", tool_input={"command": "make deploy"})
        th = threading.Thread(target=lambda: result.setdefault("d", H.forward("codex", req, path)))
        th.start()
        for _ in range(200):
            if svc.tracker.pending():
                break
            threading.Event().wait(0.01)
        row = svc.tracker.state_message()["agents"][0]
        assert row["m"] == "wait" and row["d"] == "Bash: make deploy" and row["q"]
        assert svc.handle_decide({"id": row["q"], "decision": "allow"}, 0.0) == []
        th.join(3)
        assert result["d"] == ("allow", [])
        assert svc.tracker.agents["codex:s1"].mode == A.TOOL and not svc.tracker.pending()
        assert svc.handle_decide({"id": row["q"], "decision": "deny"}, 0.0)[0]["text"] == "REQUEST GONE"
    finally:
        server.stop()


def test_permission_falls_back_to_the_terminal(tmp_path, monkeypatch):
    svc, server, path = socket_service(tmp_path, monkeypatch, approve_wait_s=0.2)
    try:
        req = ev("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})
        assert H.forward("claude", req, path) == (None, [])       # timed out
        assert svc.tracker.agents["claude:s1"].mode == A.WAIT and not svc.tracker.pending()
        svc.device_online = None
        svc._device_online = lambda: False
        assert H.forward("claude", ev("PermissionRequest", session="s2"), path) == (None, [])   # pad offline
    finally:
        server.stop()


def test_pad_asks_for_the_terminal(tmp_path, monkeypatch):
    svc, server, path = socket_service(tmp_path, monkeypatch, approve_wait_s=5.0)
    try:
        result = {}
        th = threading.Thread(target=lambda: result.setdefault(
            "d", H.forward("claude", ev("PermissionRequest", tool_name="Bash", tool_input={"command": "x"}), path)))
        th.start()
        for _ in range(200):
            if svc.tracker.pending():
                break
            threading.Event().wait(0.01)
        q = svc.tracker.state_message()["agents"][0]["q"]
        svc.handle_decide({"id": q, "decision": "ask"}, 0.0)
        th.join(3)
        assert result["d"] == (None, [])
    finally:
        server.stop()


def test_hook_main_is_silent_without_a_daemon(tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setenv("MACROPADD_AGENTS_SOCKET", str(tmp_path / "nothing.sock"))
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(ev("PermissionRequest")).encode())))
    assert H.main(["claude"]) == 0
    assert capsys.readouterr().out == ""


def test_decision_output_shape():
    assert H.decision_output("allow") == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                                 "decision": {"behavior": "allow"}}}
    assert H.decision_output("deny")["hookSpecificOutput"]["decision"]["message"]


def test_codex_internal_prompts_ignored():
    t = A.AgentTracker(Clock())
    assert t.ingest("codex", ev("UserPromptSubmit", prompt="Generate 0 to 3 hyperpersonalized suggestions")) is None
    assert not t.agents


def test_idle_prompt_after_work_means_finished():
    """Seen live: Stop lost or overridden, then Claude's 60 s idle notification
    must not turn the session amber."""
    t = A.AgentTracker(Clock())
    a = t.ingest("claude", ev("PostToolUse", tool_response={}))
    t.ingest("claude", ev("Notification", notification_type="idle_prompt", message="Claude is waiting for your input"))
    assert a.mode == A.DONE


def test_notification_never_creates_a_session():
    """Seen live: Claude notified under the conversation's old session id."""
    t = A.AgentTracker(Clock())
    assert t.ingest("claude", ev("Notification", session="old", notification_type="agent_needs_input")) is None
    assert not t.agents
    assert t.log[-1]["event"] == "Notification" and not t.log[-1]["applied"]


SUGGESTIONS = [
    {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test"}], "behavior": "allow",
     "destination": "localSettings"},
    {"type": "setMode", "mode": "acceptEdits", "destination": "session"},
]


def test_always_rules_are_claude_allow_rules_only():
    assert A.always_rules("claude", {"permission_suggestions": SUGGESTIONS}) == SUGGESTIONS[:1]
    assert A.always_rules("codex", {"permission_suggestions": SUGGESTIONS}) == []
    assert A.always_rules("claude", {}) == []


def test_always_from_the_pad_saves_claudes_rule(tmp_path, monkeypatch):
    svc, server, path = socket_service(tmp_path, monkeypatch, approve_wait_s=5.0)
    try:
        result = {}
        req = ev("PermissionRequest", tool_name="Bash", tool_input={"command": "npm test"},
                 permission_suggestions=SUGGESTIONS)
        th = threading.Thread(target=lambda: result.setdefault("d", H.forward("claude", req, path)))
        th.start()
        for _ in range(200):
            if svc.tracker.pending():
                break
            threading.Event().wait(0.01)
        row = svc.tracker.state_message()["agents"][0]
        assert row["a"] == 1                                    # the pad may offer "always"
        svc.handle_decide({"id": row["q"], "decision": "always"}, 0.0)
        th.join(3)
        assert result["d"] == ("allow", SUGGESTIONS[:1])
        out = H.decision_output(*result["d"])["hookSpecificOutput"]["decision"]
        assert out == {"behavior": "allow", "updatedPermissions": SUGGESTIONS[:1]}
    finally:
        server.stop()


def test_auto_mode_offered_from_asking_modes_and_sent_as_set_mode():
    t = A.AgentTracker(Clock())
    perm = t.ask("claude", ev("PermissionRequest", tool_name="Bash", tool_input={"command": "x"},
                              permission_mode="default"))
    assert perm.auto_ok and t.state_message()["agents"][0]["u"] == 1
    t.decide(perm.id, "auto")
    assert perm.decision == "allow" and perm.updates == [A.AUTO_MODE]
    assert not A.can_switch_to_auto("claude", {"permission_mode": "auto"})
    assert not A.can_switch_to_auto("codex", {"permission_mode": "default"})
    codex = t.ask("codex", ev("PermissionRequest", session="s2"))
    t.decide(codex.id, "auto")
    assert codex.decision == "allow" and codex.updates == []          # plain yes for Codex


def test_session_start_comes_from_the_transcript(tmp_path):
    claude = tmp_path / "claude.jsonl"
    claude.write_text('{"type":"mode","mode":"normal"}\n{"type":"file-history-snapshot","snapshot":{"timestamp":"x"}}\n'
                      '{"type":"user","timestamp":"2026-10-24T17:54:08.255Z"}\n')
    codex = tmp_path / "codex.jsonl"
    codex.write_text('{"timestamp":"2026-10-24T17:30:03.508Z","type":"session_meta"}\n')
    assert A.session_started(str(claude)) == 1792864448.255
    assert A.session_started(str(codex)) == 1792863003.508
    assert A.session_started(str(tmp_path / "missing")) is None and A.session_started(None) is None
    clock = Clock(1792864448.255 + 600)
    t = A.AgentTracker(clock)
    t.ingest("claude", ev("UserPromptSubmit", transcript_path=str(claude)))
    assert t.state_message()["agents"][0]["st"] == 600
    t.ingest("codex", ev("SessionStart", session="c2"))                  # no transcript: first seen
    clock.t += 30
    t.ingest("codex", ev("UserPromptSubmit", session="c2"))
    assert t.state_message()["agents"][1]["st"] == 30


def test_token_counter_reads_increments(tmp_path):
    claude = tmp_path / "c.jsonl"
    usage = '{"type":"assistant","message":{"id":"m1","usage":{"input_tokens":2,"cache_creation_input_tokens":10,' \
            '"cache_read_input_tokens":100,"output_tokens":7}}}\n'
    claude.write_text('{"type":"user","timestamp":"2026-10-24T17:54:08Z"}\n' + usage + usage)   # streamed twice
    c = A.Transcript(str(claude), "claude")
    assert c.refresh() and (c.input, c.output) == (112, 7)
    assert not c.refresh()
    with claude.open("a") as fh:
        fh.write(usage.replace('"m1"', '"m2"').replace('"output_tokens":7', '"output_tokens":3'))
        fh.write('{"type":"assistant","message":{"id":"m3","usa')                         # half written
    assert c.refresh() and (c.input, c.output) == (224, 10)
    codex = tmp_path / "x.jsonl"
    codex.write_text('{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":'
                     '{"input_tokens":500,"output_tokens":20,"reasoning_output_tokens":5}}}}\n')
    x = A.Transcript(str(codex), "codex")
    assert x.refresh() and (x.input, x.output) == (500, 25)


def test_state_carries_tokens(tmp_path):
    claude = tmp_path / "c.jsonl"
    claude.write_text('{"type":"assistant","message":{"id":"m1","usage":{"input_tokens":5,"output_tokens":2}}}\n')
    t = A.AgentTracker(Clock())
    t.ingest("claude", ev("UserPromptSubmit", transcript_path=str(claude)))
    assert "tk" not in t.state_message()["agents"][0]
    assert t.refresh_transcripts() and t.dirty
    assert t.state_message()["agents"][0]["tk"] == [5, 2]


def test_titles_from_claude_transcript_and_codex_index(tmp_path, monkeypatch):
    claude = tmp_path / "c.jsonl"
    claude.write_text('{"type":"ai-title","aiTitle":"Fix the build","sessionId":"s1"}\n')
    c = A.Transcript(str(claude), "claude", "s1")
    assert c.refresh() and c.title == "Fix the build"
    with claude.open("a") as fh:
        fh.write('{"type":"custom-title","customTitle":"release 2.0","sessionId":"s1"}\n'
                 '{"type":"ai-title","aiTitle":"Something else","sessionId":"s1"}\n')
    assert c.refresh() and c.title == "release 2.0"            # /rename wins over the AI title
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "session_index.jsonl").write_text(
        '{"id":"x1","thread_name":"Old name"}\n{"id":"x2","thread_name":"Other"}\n{"id":"x1","thread_name":"Show today"}\n')
    x = A.Transcript(str(tmp_path / "missing.jsonl"), "codex", "x1")
    assert x.refresh() and x.title == "Show today"
    assert not x.refresh()                                      # index unchanged: not re-read


def test_pad_gets_session_title_not_prompts_or_replies(tmp_path):
    t = A.AgentTracker(Clock())
    t.ingest("claude", ev("UserPromptSubmit", prompt="first prompt"))
    t.ingest("claude", ev("UserPromptSubmit", prompt="second prompt"))
    t.ingest("claude", ev("Stop", last_assistant_message="Here is my long reply."))
    row = t.state_message()["agents"][0]
    assert row["t"] == "" and "d" not in row                   # no title yet: nothing, never a prompt
    claude = tmp_path / "c.jsonl"
    claude.write_text('{"type":"ai-title","aiTitle":"Make it work"}\n')
    t.ingest("claude", ev("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}, transcript_path=str(claude)))
    t.refresh_transcripts()
    row = t.state_message()["agents"][0]
    assert row["t"] == "Make it work" and row["d"] == "Bash: ls"


def test_usage_from_claude_status_line_and_codex_transcript(tmp_path, monkeypatch):
    clock = Clock(1000.0)
    svc = A.AgentService(A.AgentTracker(clock))
    svc.on_socket_message({"provider": "claude", "status": {"rate_limits": {
        "five_hour": {"used_percentage": 12.4, "resets_at": 1000 + 7800},
        "seven_day": {"used_percentage": 34, "resets_at": 1000 + 86400 * 3}}}})
    codex = tmp_path / "x.jsonl"
    codex.write_text('{"type":"event_msg","payload":{"type":"token_count","info":null,"rate_limits":{'
                     '"primary":{"used_percent":3.0,"window_minutes":300,"resets_at":1500},'
                     '"secondary":{"used_percent":9.0,"window_minutes":10080,"resets_at":5000}}}}\n')
    svc.tracker.ingest("codex", ev("UserPromptSubmit", transcript_path=str(codex)))
    svc.tracker.refresh_transcripts()
    u = svc.tracker.state_message()["u"]
    assert u == {"C": [12, 7800, 34, 259200], "X": [3, 500, 9, 4000]}
    svc.on_socket_message({"provider": "claude", "status": {"rate_limits": None}})   # API key users: nothing
    assert svc.tracker.usage["C"]["5h"][0] == 12.4


def test_hook_status_mode_forwards_limits_and_prints_nothing(tmp_path, monkeypatch, capsys):
    import io
    svc, server, path = socket_service(tmp_path, monkeypatch)
    try:
        data = {"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": 0}}}
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(data).encode())))
        assert H.main(["claude", "status"]) == 0
        for _ in range(100):
            if svc.tracker.usage:
                break
            threading.Event().wait(0.01)
        assert svc.tracker.usage["C"]["5h"] == (50.0, 0.0) and "5h" in capsys.readouterr().out
    finally:
        server.stop()


def test_status_line_text(tmp_path):
    import re
    data = {"model": {"display_name": "Opus 5.5"}, "context_window": {"used_percentage": 34.2},
            "rate_limits": {"five_hour": {"used_percentage": 13, "resets_at": 1000 + 3 * 3600 + 18 * 60},
                            "seven_day": {"used_percentage": 85.4, "resets_at": 1000 + 5 * 86400 + 8 * 3600}}}
    line = H.status_line(data, now=1000, voice="hold")
    plain = re.sub(r"\033\[[0-9;]*m", "", line)
    assert plain == "Opus 5.5 · ctx 34% · 5h 13% (3h18m) · wk 85% (5d8h) · \U0001f399 hold"
    assert "\033[31m85%" in line and "\033[32m13%" in line           # red at 80 %+, green under 50 %
    assert re.sub(r"\033\[[0-9;]*m", "", H.status_line({}, voice="")) == ""   # API-key users: nothing to show
    settings = tmp_path / "settings.json"
    settings.write_text('{"voice": {"enabled": true, "mode": "tap"}}')
    assert H.voice_mode(settings) == "tap"
    settings.write_text('{"voice": {"enabled": false}}')
    assert H.voice_mode(settings) == ""
    assert H.voice_mode(tmp_path / "missing.json") == ""
