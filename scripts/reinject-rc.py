#!/usr/bin/env python3
"""Re-inject apps/master-rocketchat.py into per-agent apps/rocketchat.py while
preserving each agent's existing DEFAULT_* constants (channel, user, webhook,
tmux session, interval, system prompt).

Use this whenever the master template changes (auth flow, bug fix, new feature)
and you want every existing agent to pick it up without re-running the full
deploy pipeline (which would touch the channel / webhook / sandbox / context).

Usage
-----
    python3 scripts/reinject-rc.py            # all agents under agents/
    python3 scripts/reinject-rc.py NAME ...   # only the named agents
    python3 scripts/reinject-rc.py --dry-run  # show what would change

Reads the master at apps/master-rocketchat.py, reads each agent's existing
apps/rocketchat.py to recover its constants, writes the injected master back
to apps/rocketchat.py.

Idempotent. Safe to run multiple times.
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

ROOT       = Path(__file__).resolve().parent.parent
AGENTS_DIR = ROOT / "agents"
MASTER_RC  = ROOT / "apps" / "master-rocketchat.py"

CONST_KEYS_STR = ("DEFAULT_CHANNEL", "DEFAULT_USER", "DEFAULT_WEBHOOK_URL",
                  "DEFAULT_TMUX_SESSION", "DEFAULT_SYSTEM_PROMPT")
CONST_KEYS_INT = ("DEFAULT_INTERVAL",)


def read_str_const(text: str, key: str) -> str | None:
    """Pull a string DEFAULT_* literal out of source, supporting single,
    double, triple-single, and triple-double quoting. Returns the unescaped
    value or None if the constant is not assigned to a string literal."""
    m = re.search(rf"""^{re.escape(key)}\s*=\s*(\"\"\"|'''|"|')""",
                  text, re.MULTILINE)
    if not m:
        return None
    quote = m.group(1)
    start = m.end()
    end = text.find(quote, start)
    if end == -1:
        return None
    return text[start:end]


def read_int_const(text: str, key: str) -> int | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(-?\d+)\b", text, re.MULTILINE)
    return int(m.group(1)) if m else None


def set_const(text: str, key: str, value_literal: str) -> str:
    """Replace `KEY = ...` (single line) with `KEY = <value_literal>`. The
    caller is responsible for producing a valid python literal."""
    pat = re.compile(rf"^{re.escape(key)}\s*=\s*.*$", re.MULTILINE)
    if not pat.search(text):
        raise SystemExit(
            f"FATAL: master-rocketchat.py is missing constant {key} "
            f"(injection point); check {MASTER_RC.relative_to(ROOT)}.")
    return pat.sub(f"{key} = {value_literal}", text, count=1)


def reinject(agent_dir: Path, master_text: str, dry: bool) -> dict:
    """Returns a dict {key: value, ...} of the constants that were preserved,
    or raises if the agent is missing required state."""
    target = agent_dir / "apps" / "rocketchat.py"
    if not target.is_file():
        raise FileNotFoundError(target)

    existing = target.read_text()
    preserved: dict[str, object] = {}

    out = master_text
    for key in CONST_KEYS_STR:
        val = read_str_const(existing, key)
        if val is None:
            raise RuntimeError(
                f"{target.relative_to(ROOT)}: missing string const {key} "
                f"— refusing to clobber with empty value.")
        preserved[key] = val
        out = set_const(out, key, repr(val))

    for key in CONST_KEYS_INT:
        val = read_int_const(existing, key)
        if val is None:
            raise RuntimeError(
                f"{target.relative_to(ROOT)}: missing int const {key}.")
        preserved[key] = val
        out = set_const(out, key, str(val))

    if dry:
        same = (out == existing)
        print(f"  [dry-run] {target.relative_to(ROOT)}  "
              f"({'no change' if same else f'{len(out)-len(existing):+d} bytes'})")
    else:
        target.write_text(out)
        target.chmod(0o755)
        print(f"  wrote {target.relative_to(ROOT)}")

    return preserved


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("agents", nargs="*", help="Agent names (default: all).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing.")
    p.add_argument("--show", action="store_true",
                   help="Print the preserved constants for each agent.")
    args = p.parse_args()

    if not MASTER_RC.is_file():
        print(f"FATAL: master not found at {MASTER_RC}", file=sys.stderr)
        return 2
    master_text = MASTER_RC.read_text()

    if args.agents:
        targets = [AGENTS_DIR / n for n in args.agents]
    else:
        targets = sorted(p for p in AGENTS_DIR.iterdir()
                         if p.is_dir() and not p.name.startswith("."))

    print(f"Re-injecting {MASTER_RC.relative_to(ROOT)} into "
          f"{len(targets)} agent(s){' [dry-run]' if args.dry_run else ''}")
    fails = 0
    for agent in targets:
        if not agent.is_dir():
            print(f"  SKIP missing agent dir: {agent}")
            fails += 1
            continue
        try:
            preserved = reinject(agent, master_text, args.dry_run)
        except Exception as e:
            print(f"  FAIL {agent.name}: {e}")
            fails += 1
            continue
        if args.show:
            for k, v in preserved.items():
                shown = (v if isinstance(v, int)
                         else (v[:60] + "…" if len(v) > 60 else v))
                print(f"    {k:22s} = {shown!r}")

    print(f"Done: {len(targets) - fails} ok, {fails} failed.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
