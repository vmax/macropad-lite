# macropadd (lite)

The MacroPad daemon with only **AGENTS** mode: Claude Code / Codex sessions
on the pad's keys, permission prompts answered from the pad, subscription
limits on the usage key. No worklog, OTP, plan, desk actions, volume or SEQ.

The pad runs the normal firmware; its other modes simply get no data.
Package, CLI, config path, hook socket and service unit are the same as the
full daemon, so the two are drop-in replacements (run only one per machine).

```sh
uv sync
uv run macropadd agents install     # hooks into ~/.claude/settings.json and ~/.codex/config.toml (backed up first)
uv run macropadd agent install      # start at login (LaunchAgent / systemd user unit)
uv run macropadd agents ls          # what the daemon sees
```

Optional config: `cp macropadd.example.toml ~/.config/macropadd/config.toml`.

Remove: `uv run macropadd agents uninstall && uv run macropadd agent uninstall`.

Dependencies: Python 3.12+, pyserial. The hook (`python -P -m macropadd.hook`)
is stdlib only and exits 0 whatever happens, so a missing daemon never
blocks an agent.
