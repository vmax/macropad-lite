"""Command line entry: `macropadd <command>`.

  macropadd run [-v]        connect to the device and serve
  macropadd agent install   run at login: LaunchAgent / systemd user unit (uninstall | restart | status)
  macropadd agents install  hooks into Claude Code / Codex (uninstall | status | ls)
  macropadd ports           list serial ports
  macropadd config          show resolved configuration
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime

from . import PROTOCOL, __version__
from .config import ConfigError, config_path, load


def cmd_ports(_args) -> int:
    from .serial_transport import DEFAULT_GLOB, board_ports

    ports = board_ports()
    if not ports:
        print(f"no MacroPad serial ports (Adafruit USB id or {DEFAULT_GLOB})")
        return 1
    for i, p in enumerate(ports):
        role = "console" if i == 0 else "data" if i == 1 else "?"
        print(f"{p}  ({role})")
    return 0


def cmd_config(_args) -> int:
    path = config_path()
    try:
        cfg = load(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    print(f"# {path} ({'found' if path.exists() else 'missing, using defaults'})")
    print(json.dumps(asdict(cfg), indent=2, default=str))
    return 0


def setup_logging(level: str, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, level, logging.INFO),
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def cmd_run(args) -> int:
    from .daemon import Daemon

    try:
        cfg = load(config_path())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    setup_logging(cfg.log_level, args.verbose)
    return Daemon(cfg).run()


def cmd_agent(args) -> int:
    from . import agent

    if args.op == "install":
        print(agent.install())
    elif args.op == "uninstall":
        print(agent.uninstall())
    elif args.op == "restart":
        print(agent.restart())
    else:
        print(agent.status())
    return 0


def cmd_agents(args) -> int:
    from . import agent_hooks
    from .agents import query

    if args.op == "ls":
        rows = query()
        if rows is None:
            print("daemon not reachable (macropadd run / agent status)", file=sys.stderr)
            return 1
        usage = query(what="usage") or {}
        for letter, name in (("C", "claude"), ("X", "codex")):
            u = usage.get(letter)
            if u:
                parts = [f"{w} {u[i]}% (resets in {u[i + 1] // 3600}h{u[i + 1] % 3600 // 60:02d}m)"
                         for i, w in ((0, "5h"), (2, "week")) if u[i] >= 0]
                print(f"{name} limits: " + ", ".join(parts))
        if not rows:
            print("no agents")
        for r in rows:
            key = "-" if r["slot"] is None else str(r["slot"])
            print(f"{key} {r['provider']:<6} {r['mode']:<5} {r['age_s']:>5}s  {r['name']:<16} {r['title'][:40]}")
            if r.get("tokens"):
                print(f"    tokens in {r['tokens'][0]:,} out {r['tokens'][1]:,}")
            if r["detail"]:
                print(f"    {r['detail']}")
            for p in r["pending"]:
                print(f"    waiting for the pad: {p}")
        return 0
    if args.op == "status":
        print(agent_hooks.status())
        return 0
    if args.op == "log":
        rows = query(what="log")
        if rows is None:
            print("daemon not reachable (macropadd run / agent status)", file=sys.stderr)
            return 1
        for r in rows[-60:]:
            stamp = datetime.fromtimestamp(r["t"]).strftime("%H:%M:%S")
            mark = "" if r["applied"] else "  (ignored)"
            print(f"{stamp} {r['provider']:<6} {r['session'][:8]} {r['event']:<18} {r['extra'][:18]:<18} -> {r['mode']}{mark}")
        return 0
    try:
        cfg = load(config_path())
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    which = args.which or "all"
    results = (agent_hooks.install(which, cfg.agents_approve_wait_s) if args.op == "install"
               else agent_hooks.uninstall(which))
    for r in results:
        print(r)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="macropadd", description=__doc__)
    ap.add_argument("--version", action="version", version=f"macropadd {__version__} protocol {PROTOCOL}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list candidate serial ports").set_defaults(fn=cmd_ports)
    sub.add_parser("config", help="show the resolved configuration").set_defaults(fn=cmd_config)
    run = sub.add_parser("run", help="run the daemon")
    run.add_argument("-v", "--verbose", action="store_true", help="log every message")
    run.set_defaults(fn=cmd_run)
    agn = sub.add_parser("agents", help="AGENTS mode hooks: install | uninstall | status | ls | log")
    agn.add_argument("op", choices=["install", "uninstall", "status", "ls", "log"])
    agn.add_argument("which", nargs="?", choices=["claude", "codex"], help="one agent only (default: both)")
    agn.set_defaults(fn=cmd_agents)
    ag = sub.add_parser("agent", help="run at login (LaunchAgent / systemd --user): install | uninstall | restart | status")
    ag.add_argument("op", choices=["install", "uninstall", "restart", "status"])
    ag.set_defaults(fn=cmd_agent)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
