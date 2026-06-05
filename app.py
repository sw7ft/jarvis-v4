#!/usr/bin/env python3
"""
JARVIS v4 Dashboard — single-file Flask web UI.
Run: python3 app.py  →  http://localhost:5112
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import fcntl
import pty
import select
import struct
import termios

from flask import Flask, Response, jsonify, render_template_string, request
from flask_sock import Sock

app  = Flask(__name__)
sock = Sock(app)

JARVIS_ROOT  = Path(__file__).resolve().parent
AGENTS_DIR   = JARVIS_ROOT / "agents"
APPS_DIR     = JARVIS_ROOT / "apps"
ARCHIVE_DIR  = JARVIS_ROOT / "archive"
DEPLOY_PY    = JARVIS_ROOT / "deploy.py"

# v2 -> v4 migration helper (pure module, safe to import at startup)
import migrate_v2  # noqa: E402

# ── Master Rocket.Chat client (lazy-init, PAT-cached) ──────────────────────────
# Used by /api/rocketchat/feed for the global message viewer. We deliberately
# load the master module dynamically (not as a sibling import) because its
# filename uses a hyphen (`master-rocketchat.py`) which isn't a legal Python
# module name. The client is built once on first use; rebuild on failure.
import importlib.util as _ilu  # noqa: E402
import threading as _threading  # noqa: E402

try:
    _rc_spec = _ilu.spec_from_file_location("_rc_master_mod",
                                            APPS_DIR / "master-rocketchat.py")
    _rc_master_mod = _ilu.module_from_spec(_rc_spec)
    _rc_spec.loader.exec_module(_rc_master_mod)
except Exception as _e:  # config missing, httpx not installed, etc.
    _rc_master_mod = None
    print(f"  [warn] master-rocketchat.py not importable: {_e}")

_rc_client = None
_rc_client_err: str | None = None
_rc_client_lock = _threading.Lock()
_rc_feed_cache: dict = {"ts": 0.0, "data": None}  # 5s server-side memo
_RC_FEED_TTL = 5.0


def _get_rc_client():
    """Return a singleton authenticated Rocket.Chat client (PAT-based) or None.

    Builds lazily so a missing/borked config doesn't crash app startup. If
    construction fails the error is cached but a future call will try again
    (fresh credentials on disk → recovery without restart).
    """
    global _rc_client, _rc_client_err
    if _rc_client is not None:
        return _rc_client
    if _rc_master_mod is None:
        return None
    with _rc_client_lock:
        if _rc_client is not None:
            return _rc_client
        try:
            _rc_client = _rc_master_mod.RocketChat.from_config()
            _rc_client_err = None
        except Exception as e:
            _rc_client_err = str(e)
            print(f"  [warn] RC client init failed: {e}")
            _rc_client = None
        return _rc_client

# ── Cursor agent model selection ──────────────────────────────────────────────
# Per-agent override file at agents/<name>/.cursor-model (single line, slug).
# Absent file → DEFAULT_MODEL. The dashboard's Agent Settings popup writes
# this file via /api/agent/model/<name> and asks the user to restart.
DEFAULT_MODEL = "composer-2.5"

# Curated set surfaced in the dropdown — full list (60+) is overkill.
# Order matters: rendered in the <select> top-to-bottom.
MODEL_CHOICES = [
    {"slug": "composer-2.5",                      "label": "Composer 2.5 (default)"},
    {"slug": "composer-2",                        "label": "Composer 2"},
    {"slug": "composer-2-fast",                   "label": "Composer 2 Fast"},
    {"slug": "claude-4.6-sonnet-medium",          "label": "Sonnet 4.6 1M"},
    {"slug": "claude-4.6-sonnet-medium-thinking", "label": "Sonnet 4.6 1M Thinking"},
    {"slug": "claude-4.5-opus-high",              "label": "Opus 4.5 High"},
    {"slug": "gpt-5.2",                           "label": "GPT-5.2"},
]
_MODEL_SLUGS = {m["slug"] for m in MODEL_CHOICES}


def read_agent_model(agent_dir: Path) -> str:
    """Return the model slug for an agent (override file or DEFAULT_MODEL).

    Mirrors the helper of the same name in deploy.py — duplicated rather than
    imported so app.py never touches deploy.py's argparse/__main__ code paths.
    """
    f = agent_dir / ".cursor-model"
    if f.is_file():
        try:
            val = f.read_text().strip()
            if val:
                return val
        except Exception:
            pass
    return DEFAULT_MODEL


# ── App registry ───────────────────────────────────────────────────────────────
# Adding a new app = add one entry here + place the master .py in jarvisv4/apps/.
APPS_REGISTRY = {
    "rocketchat": {
        "label":   "Rocket.Chat",
        "master":  "master-rocketchat.py",
        "dest":    "rocketchat.py",
        "color":   "#f5455c",
        "builtin": True,   # always present on every agent
        "fields": [
            {"key": "DEFAULT_CHANNEL",     "label": "Channel",       "secret": False},
            {"key": "DEFAULT_USER",        "label": "Bot Username",  "secret": False},
            {"key": "DEFAULT_TMUX_SESSION","label": "tmux Session",  "secret": False},
            {"key": "DEFAULT_WEBHOOK_URL", "label": "Webhook URL",   "secret": False},
            {"key": "DEFAULT_INTERVAL",    "label": "Poll Interval", "secret": False},
        ],
    },
    "mailinbox": {
        "label":   "Mail Inbox",
        "master":  "mailinbox.py",
        "dest":    "mailinbox.py",
        "color":   "#38bdf8",
        "builtin": False,
        "fields": [
            {"key": "DEFAULT_HOST",     "label": "Mail Host", "secret": False},
            {"key": "DEFAULT_EMAIL",    "label": "Email",     "secret": False},
            {"key": "DEFAULT_PASSWORD", "label": "Password",  "secret": True},
            {"key": "DEFAULT_INBOX",    "label": "Inbox",     "secret": False},
        ],
    },
    "browser": {
        "label":   "Browser",
        "master":  "browser.py",
        "dest":    "browser.py",
        "color":   "#a78bfa",
        "builtin": False,
        "fields": [
            {"key": "DEFAULT_PROFILE_NAME", "label": "Profile",     "secret": False},
            {"key": "DEFAULT_PROFILE_DIR",  "label": "Profile Dir", "secret": False},
            {"key": "DEFAULT_CDP_PORT",     "label": "CDP Port",    "secret": False},
            {"key": "DEFAULT_CHROME_PATH",  "label": "Chrome Path", "secret": False},
            {"key": "DEFAULT_START_URL",    "label": "Start URL",   "secret": False},
        ],
    },
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _tmux(*args) -> tuple[int, str]:
    r = subprocess.run(["tmux"] + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def session_for(name: str) -> str:
    return name.replace(".", "-")


def rocketchat_base_url() -> str:
    """RC server URL for dashboard deep-links (env or ~/.config/rocketchat/config.json)."""
    env = os.environ.get("JARVIS_RC_URL", "").strip().rstrip("/")
    if env:
        return env
    cfg_path = Path.home() / ".config" / "rocketchat" / "config.json"
    if cfg_path.is_file():
        try:
            url = json.loads(cfg_path.read_text()).get("url", "").strip().rstrip("/")
            if url:
                return url
        except Exception:
            pass
    return ""


def get_sessions() -> set[str]:
    rc, out = _tmux("list-sessions", "-F", "#{session_name}")
    if rc != 0:
        return set()
    return set(out.splitlines())


def get_dispatch_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for log_path in AGENTS_DIR.glob("*/logs/dispatch.log"):
        agent = log_path.parent.parent.name
        try:
            text = log_path.read_text(errors="replace")
            n = sum(1 for line in text.splitlines()
                    if '"event": "dispatch"' in line or '"event":"dispatch"' in line)
            counts[agent] = n
        except Exception:
            counts[agent] = 0
    return counts


def _read_tags(agent_dir: Path) -> list:
    try:
        f = agent_dir / "tags.json"
        return json.loads(f.read_text()) if f.is_file() else []
    except Exception:
        return []


def _write_tags(agent_dir: Path, tags: list):
    f = agent_dir / "tags.json"
    f.write_text(json.dumps(sorted(set(tags))))


def _is_master(agent_dir: Path) -> bool:
    return (agent_dir / ".master").exists()


def _set_master(agent_dir: Path, value: bool):
    f = agent_dir / ".master"
    if value:
        f.touch()
    elif f.exists():
        f.unlink()


def _mailinbox_configured(path: Path) -> bool:
    """True only if mailinbox.py exists and has a non-empty DEFAULT_EMAIL injected."""
    import re as _re
    if not path.exists():
        return False
    m = _re.search(r"""^DEFAULT_EMAIL\s*=\s*['"](.+?)['"]""", path.read_text(), _re.MULTILINE)
    return bool(m and m.group(1).strip())


def _browser_configured(path: Path) -> bool:
    """True only if browser.py exists and has a non-empty DEFAULT_PROFILE_DIR injected.

    Mirrors `_mailinbox_configured` — the dashboard uses this to decide whether
    to show the browser icon in the card's right dock. deploy.py always injects
    a non-empty profile dir for every agent, so in practice this is True for
    any agent that's been (re)deployed after the browser app shipped.
    """
    import re as _re
    if not path.exists():
        return False
    m = _re.search(r"""^DEFAULT_PROFILE_DIR\s*=\s*['"](.+?)['"]""",
                   path.read_text(), _re.MULTILINE)
    return bool(m and m.group(1).strip())


def _browser_meta(agent_name: str) -> dict:
    """Return live state for an agent's Chrome: running?, pid, port, last_url.

    Looks up the injected DEFAULT_PROFILE_DIR + DEFAULT_CDP_PORT from the
    agent's browser.py, reads the .jarvis-meta.json that browser.py writes
    on launch, then verifies the pid + port are actually alive.
    """
    import re as _re
    bp = AGENTS_DIR / agent_name / "apps" / "browser.py"
    if not bp.exists():
        return {"installed": False}

    text = bp.read_text()
    def _grab(key: str) -> str:
        m = _re.search(rf"""^{key}\s*=\s*(.+)$""", text, _re.MULTILINE)
        return m.group(1).strip().strip("'\"") if m else ""

    profile_dir = _grab("DEFAULT_PROFILE_DIR")
    port_str    = _grab("DEFAULT_CDP_PORT")
    chrome_path = _grab("DEFAULT_CHROME_PATH")
    try:
        port = int(port_str)
    except Exception:
        port = 0

    result = {
        "installed":   True,
        "profile_dir": profile_dir,
        "port":        port,
        "chrome_path": chrome_path,
        "running":     False,
        "pid":         None,
        "headless":    None,
        "started_at":  None,
        "last_url":    None,
        "last_used":   None,
    }
    if not profile_dir:
        return result

    meta_path = Path(profile_dir) / ".jarvis-meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            meta = {}
        result.update({
            "pid":        meta.get("pid"),
            "headless":   meta.get("headless"),
            "started_at": meta.get("started_at"),
            "last_url":   meta.get("last_url"),
            "last_used":  meta.get("last_used"),
        })

    pid = result.get("pid")
    pid_alive = False
    if pid:
        try:
            os.kill(int(pid), 0)
            pid_alive = True
        except (ProcessLookupError, ValueError, TypeError):
            pid_alive = False
        except PermissionError:
            pid_alive = True

    port_open = False
    if port:
        try:
            import socket as _sk
            with _sk.create_connection(("127.0.0.1", port), timeout=0.3):
                port_open = True
        except Exception:
            port_open = False

    result["running"] = bool(pid_alive and port_open)
    return result


def monitor_heartbeat(name: str) -> dict:
    """
    Green  = a `rocketchat.py monitor --tmux-session <session>` process is alive.
    Yellow = process not found but dispatch.log / monitor.log touched < 30 min ago.
    Red    = no process and logs are stale / missing.
    Blue   = deliberately hibernated (auto-hibernate watcher killed the session
             intentionally — not a fault).
    Amber  = waking up (deploy in flight after a wake trigger).

    We match by command line (--tmux-session is unique per agent) instead of
    walking tmux pane children, because the actual process tree is usually
    pane-zsh → nested-zsh → python, and a 1-level pgrep -P misses the monitor
    sitting 2 levels deep. pgrep -f sidesteps that entirely.
    """
    # 0. Auto-hibernate state takes precedence over process-presence checks
    #    so a deliberately-asleep agent doesn't show the red 'dead' dot.
    #    `disabled` (force-off) wins over everything else for the badge.
    try:
        hdoc = _hib_load_cached()
        a    = (hdoc.get("agents", {}).get(name) or {})
        if a.get("disabled"):
            return {"status": "disabled", "age_min": -1}
        st = a.get("status")
        if st in ("hibernated", "waking"):
            return {"status": st, "age_min": -1}
    except Exception:
        pass

    session = session_for(name)

    # 1. Find a live rocketchat.py monitor with this agent's --tmux-session flag.
    # macOS pgrep -f uses BRE — no \s, no alternation. The trailing space
    # anchors the match (next arg is always --persona) so prefix-collisions
    # Require exact session name match so partial names (e.g. `foo` vs `foo-bar`) can't collide.
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"rocketchat.py monitor.*--tmux-session {session} "],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"status": "alive", "age_min": 0}
    except Exception:
        pass

    # 2. Fall back to log file recency.
    logs_dir = AGENTS_DIR / name / "logs"
    candidates = [logs_dir / "monitor.log", logs_dir / "dispatch.log"]
    mtimes = [p.stat().st_mtime for p in candidates if p.exists()]
    if not mtimes:
        return {"status": "dead", "age_min": -1}
    age_min = int((time.time() - max(mtimes)) / 60)
    status = "stale" if age_min < 30 else "dead"
    return {"status": status, "age_min": age_min}


def get_agents() -> list[dict]:
    sessions = get_sessions()
    dispatch_counts = get_dispatch_counts()
    agents = []
    for d in sorted(AGENTS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        name    = d.name
        session = session_for(name)
        online  = session in sessions
        hb      = monitor_heartbeat(name)
        # `last_activity` powers the "Recent grid" sort. dispatch.log's mtime
        # bumps on every inbound dispatch + outbound send, so it cleanly tracks
        # "active conversation"; monitor heartbeats hit monitor.log instead and
        # don't pollute the signal. 0.0 sinks empty agents to the bottom in a
        # descending sort.
        log_path      = d / "logs" / "dispatch.log"
        last_activity = log_path.stat().st_mtime if log_path.is_file() else 0.0
        agents.append({
            "name":           name,
            "session":        session,
            "online":         online,
            "dispatches":     dispatch_counts.get(name, 0),
            "last_activity":  last_activity,
            "monitor_status": hb["status"],
            "monitor_age":    hb["age_min"],
                "has_mailinbox":  _mailinbox_configured(d / "apps" / "mailinbox.py"),
                "has_browser":    _browser_configured(d / "apps" / "browser.py"),
            "tags":           _read_tags(d),
            "is_master":      _is_master(d),
            "model":          read_agent_model(d),
        })
    return agents


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/rocketchat/feed")
def api_rocketchat_feed():
    """Global Rocket.Chat message viewer feed.

    Strategy (single round-trip per refresh, ~700ms typical):
      1. subscriptions.get → server-sorted list of rooms by last-update.
      2. Top 30 rooms → fan out to *.history in parallel (12 threads).
      3. Merge, sort by ts desc, take top 25.
      4. Tag each message with its v4 agent name (when the room name matches
         a directory under agents/) so the frontend can deep-link straight
         into that agent's settings popup.

    A 5-second server-side cache absorbs multi-tab polling and prevents
    hammering Rocket.Chat when several browser tabs are open.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = time.time()
    cached = _rc_feed_cache.get("data")
    if cached and (now - _rc_feed_cache["ts"]) < _RC_FEED_TTL:
        return jsonify(cached)

    rc = _get_rc_client()
    if rc is None:
        return jsonify({
            "ok": False,
            "error": _rc_client_err or "Rocket.Chat client not configured.",
            "messages": [],
        })

    # Read bot username for "this is me" labelling on messages.
    me_username = ""
    try:
        cfg_path = Path.home() / ".config" / "rocketchat" / "config.json"
        if cfg_path.is_file():
            cfg = json.loads(cfg_path.read_text())
            me_username = (cfg.get("bot_username") or "").strip()
    except Exception:
        pass

    try:
        subs = rc.list_subscriptions()
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"subscriptions.get failed: {e}",
            "messages": [],
        })

    valid = [s for s in subs
             if isinstance(s, dict)
             and s.get("t") in ("c", "p", "d")
             and s.get("rid")]
    valid.sort(key=lambda s: s.get("_updatedAt", ""), reverse=True)
    top_rooms = valid[:30]

    # Map of channel-name → agent-name for deep-linking.
    try:
        agent_dirs = {p.name for p in AGENTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")}
    except Exception:
        agent_dirs = set()

    def _fetch(sub: dict) -> list[dict]:
        rid  = sub["rid"]
        t    = sub["t"]
        name = sub.get("name") or sub.get("fname") or rid
        try:
            msgs = rc.get_room_history(rid, t, count=5)
        except Exception:
            return []
        agent = name if name in agent_dirs else None
        for m in msgs:
            m["_room_name"] = name
            m["_room_type"] = t
            m["_agent"]     = agent
        return msgs

    all_msgs: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(_fetch, s): s for s in top_rooms}
        for f in as_completed(futs, timeout=15):
            try:
                all_msgs.extend(f.result())
            except Exception:
                pass

    all_msgs.sort(key=lambda m: m.get("ts", ""), reverse=True)
    top = all_msgs[:25]

    out = []
    for m in top:
        out.append({
            "ts":        m.get("ts", ""),
            "username":  (m.get("u") or {}).get("username") or "?",
            "msg":       (m.get("msg") or "").strip(),
            "room_name": m["_room_name"],
            "room_type": m["_room_type"],
            "agent":     m["_agent"],
        })

    data = {
        "ok":          True,
        "messages":    out,
        "me_username": me_username,
        "fetched_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rooms_seen":  len(top_rooms),
    }
    _rc_feed_cache["ts"]   = now
    _rc_feed_cache["data"] = data
    return jsonify(data)


@app.route("/api/agents")
def api_agents():
    return jsonify(get_agents())


@app.route("/api/pane/snapshots")
def api_pane_snapshots():
    sessions = request.args.getlist("session")
    result = {}
    for sess in sessions:
        target = f"{sess}:main.1"
        rc, out = _tmux("capture-pane", "-t", target, "-p", "-S", "-40")
        result[sess] = out if rc == 0 else ""
    return jsonify(result)


# ── Lifecycle helpers (shared by /api/stop, /api/start, and the
#    auto-hibernate daemon below). Kept thin so behaviour stays identical
#    whether the trigger is a user click or the watcher.
def _kill_tmux_session(name: str) -> tuple[bool, str]:
    """Tear down an agent's tmux session. Idempotent: missing session = OK."""
    session = session_for(name)
    rc, out = _tmux("kill-session", "-t", session)
    return rc == 0, out


def _run_deploy(name: str) -> tuple[bool, str, str]:
    """Run deploy.py for an existing agent (no SSH/DNS work, just relaunch)."""
    r = subprocess.run(
        ["python3", str(DEPLOY_PY), name, "--no-attach"],
        capture_output=True, text=True, cwd=str(JARVIS_ROOT)
    )
    return r.returncode == 0, r.stdout, r.stderr


@app.route("/api/stop", methods=["POST"])
def api_stop():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok, out = _kill_tmux_session(name)
    return jsonify({"ok": ok, "output": out})


@app.route("/api/start", methods=["POST"])
def api_start():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok, stdout, stderr = _run_deploy(name)
    if ok:
        # Sleep/Start round-trip consistency: if this agent was hibernated
        # (by the Sleep button or the auto-hibernate watcher), the doc still
        # says status='hibernated' and monitor_heartbeat() will short-circuit
        # to a blue badge forever. Clear the flag here so the dashboard flips
        # back to green on its next /api/agents poll. Setting wake_completed_at
        # gives the next watcher tick the same grace floor a /wake would.
        try:
            with _hibernation_lock:
                doc = _hib_load()
                a   = doc.get("agents", {}).get(name)
                if a and a.get("status") in ("hibernated", "waking"):
                    a["status"]            = "running"
                    a["hibernated_at"]     = None
                    a["waking_started"]    = None
                    a["wake_completed_at"] = _hib_now_iso()
                    _hib_audit("manual_start_cleared_hibernation", agent=name)
                    _hib_save(doc)
        except Exception:
            pass
    return jsonify({"ok": ok, "stdout": stdout, "stderr": stderr})


@app.route("/api/rc/config/<name>")
def api_rc_config(name: str):
    """Read the injected DEFAULT_* constants from the agent's rocketchat.py."""
    rc_path = AGENTS_DIR / name / "apps" / "rocketchat.py"
    if not rc_path.exists():
        return jsonify({"error": "rocketchat.py not found"}), 404
    keys = ["DEFAULT_CHANNEL", "DEFAULT_USER", "DEFAULT_INTERVAL",
            "DEFAULT_WEBHOOK_URL", "DEFAULT_TMUX_SESSION"]
    import re
    result = {}
    text = rc_path.read_text()
    for key in keys:
        m = re.search(rf"^{key}\s*=\s*(.+)$", text, re.MULTILINE)
        if m:
            val = m.group(1).strip().strip("'\"")
            result[key] = val
    return jsonify(result)


@app.route("/api/rc/kill", methods=["POST"])
def api_rc_kill():
    """Kill the rocketchat.py monitor process running in pane 2 of the agent's tmux session."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    session = session_for(name)
    # Find pane 2 pid and walk children for rocketchat.py monitor
    rc, out = _tmux("list-panes", "-t", f"{session}:main",
                    "-F", "#{pane_index} #{pane_pid}")
    killed = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "2":
            children = subprocess.run(
                ["pgrep", "-P", parts[1]], capture_output=True, text=True
            ).stdout.split()
            for cpid in children:
                cmd = subprocess.run(
                    ["ps", "-p", cpid, "-o", "command="],
                    capture_output=True, text=True
                ).stdout
                if "rocketchat.py" in cmd and "monitor" in cmd:
                    subprocess.run(["kill", cpid])
                    killed.append(cpid)
    return jsonify({"ok": True, "killed": killed})


@app.route("/api/rc/restart", methods=["POST"])
def api_rc_restart():
    """Kill the monitor then relaunch it in pane 2."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    # Kill first
    import requests as _req
    with app.test_client() as c:
        c.post("/api/rc/kill", json={"name": name})
    session = session_for(name)
    agent_dir = AGENTS_DIR / name
    rc_path   = agent_dir / "apps" / "rocketchat.py"
    if not rc_path.exists():
        return jsonify({"error": "rocketchat.py not found"}), 404
    # Re-read constants to rebuild the monitor command
    import re, shlex
    text = rc_path.read_text()
    def _const(key, default=""):
        m = re.search(rf"^{key}\s*=\s*(.+)$", text, re.MULTILINE)
        return m.group(1).strip().strip("'\"") if m else default
    channel  = _const("DEFAULT_CHANNEL", f"#{name}")
    interval = _const("DEFAULT_INTERVAL", "10")
    session_name = _const("DEFAULT_TMUX_SESSION", session_for(name))
    persona  = _const("DEFAULT_SYSTEM_PROMPT", "You are a JARVIS v4 agent.")
    log_path = agent_dir / "logs" / "monitor.log"
    monitor_cmd = (
        f'cd {agent_dir} && '
        f'PYTHONUNBUFFERED=1 python3 apps/rocketchat.py monitor "{channel}" '
        f'--interval {interval} --tmux-session {session_name} '
        f'--persona {shlex.quote(persona)} '
        f'>> {log_path} 2>&1 & '
        f'MON_PID=$!; '
        f'echo "Monitor restarted PID: $MON_PID"; '
        f'trap "kill $MON_PID 2>/dev/null" EXIT HUP TERM INT'
    )
    _tmux("send-keys", "-t", f"{session}:main.2", monitor_cmd, "Enter")
    return jsonify({"ok": True})


@app.route("/api/mailinbox/config/<name>")
def api_mailinbox_config(name: str):
    """Read injected DEFAULT_* constants from the agent's mailinbox.py."""
    mb_path = AGENTS_DIR / name / "apps" / "mailinbox.py"
    if not mb_path.exists():
        return jsonify({"error": "mailinbox.py not found"}), 404
    import re as _re
    keys = ["DEFAULT_HOST", "DEFAULT_EMAIL", "DEFAULT_INBOX",
            "DEFAULT_IMAP_PORT", "DEFAULT_SMTP_PORT"]
    result = {}
    text = mb_path.read_text()
    for key in keys:
        m = _re.search(rf"^{key}\s*=\s*(.+)$", text, _re.MULTILINE)
        if m:
            result[key] = m.group(1).strip().strip("'\"")
    return jsonify(result)


@app.route("/api/mailinbox/test", methods=["POST"])
def api_mailinbox_test():
    """Run mailinbox.py test for the given agent and return output."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    mb_path = AGENTS_DIR / name / "apps" / "mailinbox.py"
    if not mb_path.exists():
        return jsonify({"error": "mailinbox.py not found"}), 404
    r = subprocess.run(
        ["python3", str(mb_path), "test"],
        capture_output=True, text=True, timeout=20,
        cwd=str(AGENTS_DIR / name)
    )
    output = (r.stdout + r.stderr).strip()
    return jsonify({"ok": r.returncode == 0, "output": output})


# ── Browser app routes ────────────────────────────────────────────────────────
# Mirror of the mailinbox routes. The dashboard popover hits these to:
#   - Read injected config + live process state    (GET  /api/browser/config/<name>)
#   - Launch / stop / test Chrome for an agent     (POST /api/browser/{launch,stop,test})
#   - Drive a navigation from the dashboard        (POST /api/browser/goto)
#   - Pull a fresh screenshot for the preview      (GET  /api/browser/screenshot/<name>)
#
# Every endpoint shells out to the per-agent browser.py — same pattern as mail,
# so all the actual Chrome-driving logic stays in one file.

def _browser_py(name: str) -> Path:
    return AGENTS_DIR / name / "apps" / "browser.py"


def _run_browser(name: str, args: list[str], timeout: int = 60) -> tuple[bool, str]:
    """Invoke the per-agent browser.py with --json. Returns (ok, raw_stdout_or_stderr)."""
    bp = _browser_py(name)
    if not bp.exists():
        return False, "browser.py not installed for this agent"
    try:
        r = subprocess.run(
            ["python3", str(bp), "--json", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(AGENTS_DIR / name),
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    output = (r.stdout + r.stderr).strip()
    return (r.returncode == 0), output


@app.route("/api/browser/config/<name>")
def api_browser_config(name: str):
    """Return injected constants + live state (pid, port up, last url)."""
    meta = _browser_meta(name)
    if not meta.get("installed"):
        return jsonify({"error": "browser.py not found"}), 404
    return jsonify(meta)


@app.route("/api/browser/launch", methods=["POST"])
def api_browser_launch():
    body = request.json or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    args = ["launch"]
    if body.get("headless"):
        args.append("--headless")
    ok_flag, output = _run_browser(name, args, timeout=45)
    return jsonify({"ok": ok_flag, "output": output})


@app.route("/api/browser/stop", methods=["POST"])
def api_browser_stop():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok_flag, output = _run_browser(name, ["stop"], timeout=15)
    return jsonify({"ok": ok_flag, "output": output})


@app.route("/api/browser/test", methods=["POST"])
def api_browser_test():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ok_flag, output = _run_browser(name, ["test"], timeout=60)
    return jsonify({"ok": ok_flag, "output": output})


@app.route("/api/browser/goto", methods=["POST"])
def api_browser_goto():
    body = request.json or {}
    name = body.get("name", "").strip()
    url  = body.get("url", "").strip()
    if not name or not url:
        return jsonify({"error": "name and url required"}), 400
    ok_flag, output = _run_browser(name, ["goto", url], timeout=60)
    return jsonify({"ok": ok_flag, "output": output})


@app.route("/api/browser/screenshot/<name>")
def api_browser_screenshot(name: str):
    """Take a fresh screenshot and stream the PNG back. ~2-3s typical."""
    bp = _browser_py(name)
    if not bp.exists():
        return jsonify({"error": "browser.py not found"}), 404
    out_dir = AGENTS_DIR / name / "browser-profile" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dashboard-preview.png"
    try:
        r = subprocess.run(
            ["python3", str(bp), "--json", "screenshot", "--path", str(out_path)],
            capture_output=True, text=True, timeout=45,
            cwd=str(AGENTS_DIR / name),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "screenshot timed out"}), 504
    if r.returncode != 0 or not out_path.is_file():
        return jsonify({"error": (r.stdout + r.stderr).strip() or "screenshot failed"}), 500
    return Response(
        out_path.read_bytes(),
        mimetype="image/png",
        headers={"Cache-Control": "no-store"},
    )


def _read_app_constants(agent_name: str, app_id: str) -> dict:
    """Read all DEFAULT_* constants from an agent's installed app file."""
    import re as _re
    spec = APPS_REGISTRY.get(app_id, {})
    if not spec:
        return {}
    dest_path = AGENTS_DIR / agent_name / "apps" / spec["dest"]
    if not dest_path.exists():
        return {}
    text = dest_path.read_text()
    result = {}
    for field in spec["fields"]:
        key = field["key"]
        m = _re.search(rf"^{key}\s*=\s*(.+)$", text, _re.MULTILINE)
        if m:
            result[key] = m.group(1).strip().strip("'\"")
    return result


def _derived_app_defaults(agent_name: str, app_id: str) -> dict:
    """Return server-side derived defaults for an app on a specific agent.

    Used in two places:
      1. /api/apps/config — when an app isn't installed yet, returns these
         so the Add App modal pre-populates the field inputs.
      2. /api/apps/install — if the user submits empty fields, these fill in.

    Only the `browser` app currently uses derivation. All its constants are
    determined entirely by the agent name (port = SHA-1 hash → 9300-9999,
    profile dir = agents/<name>/browser-profile, chrome path = system Chrome).
    Other apps return {} — fields stay blank for the user to fill.
    """
    if app_id != "browser":
        return {}

    import hashlib as _hl
    import platform as _pl
    h = _hl.sha1(agent_name.encode("utf-8")).digest()
    port = 9300 + (int.from_bytes(h[:4], "big") % 700)
    profile_dir = AGENTS_DIR / agent_name / "browser-profile"

    sysname = _pl.system().lower()
    cands: list[str] = []
    if "darwin" in sysname:
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif "linux" in sysname:
        cands = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    chrome_path = next((c for c in cands if Path(c).is_file()), "")

    return {
        "DEFAULT_PROFILE_NAME": agent_name,
        "DEFAULT_PROFILE_DIR":  str(profile_dir),
        "DEFAULT_CDP_PORT":     str(port),
        "DEFAULT_CHROME_PATH":  chrome_path,
        "DEFAULT_START_URL":    "about:blank",
    }


def inject_app(agent_name: str, app_id: str, field_values: dict) -> tuple[bool, str]:
    """
    Copy master app file into agent's apps/ and inject field_values as DEFAULT_* constants.
    Does NOT touch tmux — safe to call on a running agent.
    Returns (ok, message).
    """
    import re as _re
    spec = APPS_REGISTRY.get(app_id)
    if not spec:
        return False, f"Unknown app: {app_id}"

    master = APPS_DIR / spec["master"]
    if not master.exists():
        return False, f"Master file not found: {master}"

    agent_dir = AGENTS_DIR / agent_name
    if not agent_dir.is_dir():
        return False, f"Agent not found: {agent_name}"

    dest = agent_dir / "apps" / spec["dest"]
    dest.parent.mkdir(parents=True, exist_ok=True)

    src = master.read_text()

    def set_const(text: str, key: str, raw_val: str) -> str:
        pat = _re.compile(rf"^{key}\s*=\s*.*$", _re.MULTILINE)
        if not pat.search(text):
            return text
        return pat.sub(f"{key} = {repr(raw_val)}", text, count=1)

    out = src
    for field in spec["fields"]:
        key = field["key"]
        if key in field_values:
            out = set_const(out, key, field_values[key])

    dest.write_text(out)
    dest.chmod(0o755)
    return True, f"Installed {spec['label']} → {dest.relative_to(JARVIS_ROOT)}"


# ── Apps install/config API ────────────────────────────────────────────────────

@app.route("/api/apps/registry")
def api_apps_registry():
    """Return the apps registry (label, fields, color) — safe to expose (no secrets)."""
    safe = {}
    for app_id, spec in APPS_REGISTRY.items():
        safe[app_id] = {
            "label":   spec["label"],
            "color":   spec["color"],
            "builtin": spec.get("builtin", False),
            "fields":  spec["fields"],
            "available": (APPS_DIR / spec["master"]).exists(),
        }
    return jsonify(safe)


@app.route("/api/apps/installed/<name>")
def api_apps_installed(name: str):
    """Return which apps are installed for a given agent (file exists check)."""
    agent_dir = AGENTS_DIR / name
    if not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404
    result = {}
    for app_id, spec in APPS_REGISTRY.items():
        result[app_id] = (agent_dir / "apps" / spec["dest"]).exists()
    return jsonify(result)


@app.route("/api/apps/config/<name>/<app_id>")
def api_apps_config(name: str, app_id: str):
    """Return current DEFAULT_* values from an agent's installed app (passwords masked).

    If the app isn't installed yet, falls back to `_derived_app_defaults()`
    so the Add App modal can pre-populate the form. For apps with no derivation
    (mailinbox, etc.) the form opens blank — same as before.
    """
    spec = APPS_REGISTRY.get(app_id)
    if not spec:
        return jsonify({"error": "unknown app"}), 404
    vals = _read_app_constants(name, app_id)
    if not vals:
        vals = _derived_app_defaults(name, app_id)
    # Mask secrets so they never leave the server in plaintext
    for field in spec["fields"]:
        if field.get("secret") and field["key"] in vals:
            v = vals[field["key"]]
            vals[field["key"]] = ("*" * len(v)) if v else ""
    return jsonify({"fields": spec["fields"], "values": vals})


@app.route("/api/apps/install", methods=["POST"])
def api_apps_install():
    """Copy+inject an app into an agent's apps/ directory. No tmux restart.

    Field resolution order for each DEFAULT_* constant:
      1. Whatever the user typed in the form (non-empty wins)
      2. Existing value already in the installed file (preserves config on edit)
      3. Server-derived default (for apps like `browser` whose constants can
         be computed entirely from the agent name)
    """
    body       = request.json or {}
    agent_name = body.get("agent", "").strip()
    app_id     = body.get("app_id", "").strip()
    fields     = body.get("fields", {})

    if not agent_name or not app_id:
        return jsonify({"error": "agent and app_id required"}), 400

    existing = _read_app_constants(agent_name, app_id)
    derived  = _derived_app_defaults(agent_name, app_id)
    spec     = APPS_REGISTRY.get(app_id, {})

    for field in spec.get("fields", []):
        key = field["key"]
        user_val = fields.get(key, "")
        # All-asterisks on a secret field means "unchanged"
        if field.get("secret") and user_val and all(c == "*" for c in user_val):
            fields[key] = existing.get(key, "")
            continue
        if user_val:
            continue
        # Empty user input → fall back to existing, then derived
        if existing.get(key):
            fields[key] = existing[key]
        elif derived.get(key):
            fields[key] = derived[key]

    ok_flag, msg = inject_app(agent_name, app_id, fields)
    return jsonify({"ok": ok_flag, "message": msg}), (200 if ok_flag else 500)


@app.route("/api/apps/remove", methods=["POST"])
def api_apps_remove():
    """Delete an app file from an agent's apps/ directory. Builtin apps cannot be removed."""
    body       = request.json or {}
    agent_name = body.get("agent", "").strip()
    app_id     = body.get("app_id", "").strip()
    if not agent_name or not app_id:
        return jsonify({"error": "agent and app_id required"}), 400
    spec = APPS_REGISTRY.get(app_id)
    if not spec:
        return jsonify({"error": "unknown app"}), 404
    if spec.get("builtin"):
        return jsonify({"error": f"{spec['label']} is a built-in app and cannot be removed"}), 400
    dest = AGENTS_DIR / agent_name / "apps" / spec["dest"]
    if not dest.exists():
        return jsonify({"ok": True, "message": "Already not installed"})
    dest.unlink()
    return jsonify({"ok": True, "message": f"Removed {spec['label']} from {agent_name}"})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    session = session_for(name)
    _tmux("kill-session", "-t", session)
    r = subprocess.run(
        ["python3", str(DEPLOY_PY), name, "--no-attach"],
        capture_output=True, text=True, cwd=str(JARVIS_ROOT)
    )
    return jsonify({"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr})


@app.route("/api/pane/stream/<session>")
def api_pane_stream(session: str):
    """SSE stream of pane 1 for View Log popup."""
    def generate():
        last = ""
        while True:
            target = f"{session}:main.1"
            rc, out = _tmux("capture-pane", "-t", target, "-p", "-S", "-120")
            if rc == 0 and out != last:
                last = out
                data = out.replace("\n", "\u23ce")
                yield f"data: {data}\n\n"
            time.sleep(1.5)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS v4</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark-dimmed.min.css">
<script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@9.1.6/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js"></script>
<style>
:root {
  --bg:      #0a0e17;
  --bg2:     #111827;
  --bg3:     #1a2235;
  --border:  #1e2d45;
  --text:    #cdd9e5;
  --text2:   #6b7f99;
  --green:   #22c55e;
  --red:     #ef4444;
  --yellow:  #f59e0b;
  --accent:  #38bdf8;
  --purple:  #a78bfa;
  --cyan:    #06b6d4;
  --card-w:  400px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { height: 100%; overflow: hidden; }
body {
  height: 100%; overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  display: flex;
  flex-direction: column;
}

/* ── header ── */
.hdr {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 20px;
  background: rgba(10,14,23,0.95);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  z-index: 10;
  backdrop-filter: blur(12px);
}
.hdr-left { display: flex; align-items: center; gap: 14px; }
.hdr h1 {
  font-size: 15px; font-weight: 800; letter-spacing: 2px;
  background: linear-gradient(90deg, var(--cyan), var(--purple));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.hdr-right { display: flex; align-items: center; gap: 12px; }
.hdr-clock { font-size: 11px; color: var(--text2); font-variant-numeric: tabular-nums; }
/* nav links */
.hdr-nav { display: flex; align-items: center; gap: 2px; }
.hdr-nav-link {
  font-size: 11px; font-weight: 600; color: var(--text2);
  background: none; border: none; border-radius: 6px;
  padding: 5px 12px; cursor: pointer; letter-spacing: 0.3px;
  transition: color 0.15s, background 0.15s;
}
.hdr-nav-link:hover { color: var(--text); background: rgba(255,255,255,0.05); }
.hdr-nav-link.active { color: var(--accent); background: rgba(56,189,248,0.1); }

#deploy-btn {
  font-size: 11px; font-weight: 700; color: #fff;
  background: linear-gradient(135deg, var(--accent) 0%, #0284c7 100%);
  border: none; border-radius: 7px; padding: 6px 14px;
  cursor: pointer; letter-spacing: 0.3px;
  box-shadow: 0 2px 8px rgba(56,189,248,0.3);
  transition: opacity 0.15s;
}
#deploy-btn:hover { opacity: 0.88; }

/* ── view toggle ── */
.view-toggle-group { display: flex; gap: 2px; }
.view-toggle-btn {
  width: 30px; height: 28px; border-radius: 7px;
  border: 1px solid var(--border); background: transparent;
  color: var(--text2); cursor: pointer; display: flex;
  align-items: center; justify-content: center;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.view-toggle-btn:hover { background: rgba(255,255,255,0.07); color: var(--text); }
.view-toggle-btn.active { background: rgba(56,189,248,0.12); border-color: var(--accent); color: var(--accent); }

/* compact-only / hide-compact button visibility */
[data-compact-only] { display: none !important; }
#canvas.compact-view [data-compact-only] { display: flex !important; }
#canvas.compact-view [data-hide-compact] { display: none !important; }

/* ── compact-view — ultra-small single-row pills ── */
/* Card min-width — tune if you use very long agent domain names (500px fits ~32 chars
   beside the status dot, model pill, and four action buttons). */
#canvas.compact-view { --card-w: 500px; }
#canvas.compact-view .agent-card {
  border-radius: 8px;
  padding: 0 !important;
  min-height: 0;
}
#canvas.compact-view .card-left {
  flex-direction: row; align-items: center;
  gap: 8px; padding: 5px 10px; flex: 1; min-width: 0;
}
#canvas.compact-view .card-top { flex: 1; min-width: 0; }
#canvas.compact-view .card-tags,
#canvas.compact-view .pane-preview,
#canvas.compact-view .pane-wrap,
#canvas.compact-view .pane-label,
#canvas.compact-view .plabel-row { display: none !important; }
/* Dispatch count stays in compact, just shrink it so it sits inline with
   the domain name without crowding. flex-shrink:0 keeps it on screen even
   when the name needs to ellipsize. */
#canvas.compact-view .dispatch-count {
  font-size: 9px;
  flex-shrink: 0;
  margin-left: 2px;
}
#canvas.compact-view .card-footer {
  border-top: none !important; padding: 0 !important;
  flex-direction: row; align-items: center; gap: 0;
  flex-shrink: 0;
}
#canvas.compact-view .card-actions {
  border-top: none; margin: 0; border-radius: 0 8px 8px 0;
}
#canvas.compact-view .card-btn {
  padding: 5px 8px; font-size: 9px; gap: 1px;
}
#canvas.compact-view .card-btn svg { width: 11px; height: 11px; }
#canvas.compact-view .card-btn span { display: none; }
#canvas.compact-view .card-right { display: none !important; }
#canvas.compact-view .card-name { font-size: 11px; }
#canvas.compact-view .status-dot { width: 6px; height: 6px; }


/* ── tag sub-nav ── */
#tag-bar {
  display: flex; align-items: center; gap: 6px; flex-wrap: nowrap;
  padding: 5px 14px;
  background: rgba(10,14,23,0.9);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
#tag-bar-label { font-size: 10px; font-weight: 700; color: var(--text2); letter-spacing: 1px; text-transform: uppercase; margin-right: 4px; flex-shrink: 0; }
/* tag pills scroll horizontally if many tags */
#tag-bar > .tag-pill, #tag-bar > span.tag-pill { flex-shrink: 0; }
#tag-bar-search {
  margin-left: auto; flex-shrink: 0;
  display: flex; align-items: center; gap: 6px;
  background: rgba(0,0,0,0.3); border: 1px solid var(--border);
  border-radius: 8px; padding: 4px 8px;
  transition: border-color 0.15s;
}
#tag-bar-search:focus-within { border-color: var(--accent); }
#tag-bar-search svg { color: var(--text2); flex-shrink: 0; }
#agent-search {
  background: none; border: none; outline: none;
  color: var(--text); font-size: 11px; width: 160px;
}
#agent-search::placeholder { color: var(--text2); opacity: 0.5; }
#agent-search-clear {
  background: none; border: none; color: var(--text2);
  cursor: pointer; font-size: 14px; line-height: 1; padding: 0;
  display: none;
}
#agent-search-clear.visible { display: block; }
#agent-search-clear:hover { color: var(--text); }
.tag-pill {
  font-size: 10px; font-weight: 600; padding: 3px 10px; border-radius: 20px;
  border: 1px solid var(--border); background: rgba(255,255,255,0.05);
  color: var(--text2); cursor: pointer; transition: all 0.15s;
  user-select: none;
}
.tag-pill:hover { background: rgba(56,189,248,0.12); border-color: var(--accent); color: var(--accent); }
.tag-pill.active { background: var(--accent); border-color: var(--accent); color: #000; font-weight: 700; }
.tag-pill.all-pill { border-style: dashed; }
/* card tag chips (shown on card if tagged) */
.card-tags { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 2px; }
.card-tag-chip {
  font-size: 9px; font-weight: 600; padding: 2px 7px; border-radius: 12px;
  background: rgba(56,189,248,0.12); border: 1px solid rgba(56,189,248,0.3);
  color: var(--accent); text-transform: uppercase; letter-spacing: 0.5px;
}

/* ── Deploy modal ── */
#deploy-overlay {
  display: none; position: fixed; inset: 0; z-index: 8000;
  background: rgba(0,0,0,0.65); backdrop-filter: blur(4px);
  align-items: center; justify-content: center;
}
#deploy-overlay.open { display: flex; }
#deploy-modal {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 16px; overflow: hidden;
  width: 500px; max-width: 95vw; max-height: 90vh;
  display: flex; flex-direction: column;
  box-shadow: 0 24px 70px rgba(0,0,0,0.8);
}
#deploy-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
  background: linear-gradient(135deg, rgba(56,189,248,0.12) 0%, transparent 100%);
}
#deploy-bar-title { font-size: 14px; font-weight: 700; color: var(--text1); }
#deploy-close {
  background: none; border: none; color: var(--text2);
  font-size: 20px; cursor: pointer; padding: 0; line-height: 1;
}
#deploy-close:hover { color: var(--text1); }
#deploy-form { padding: 16px 18px; overflow-y: auto; flex: 1; }
.deploy-field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.deploy-field label {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--text2);
}
.deploy-field input, .deploy-field select {
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text1); font-size: 12px;
  padding: 8px 10px; outline: none; transition: border-color 0.15s;
}
.deploy-field input:focus, .deploy-field select:focus { border-color: var(--accent); }
.deploy-section-hdr {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--accent); opacity: 0.7;
  margin: 14px 0 8px; padding-bottom: 4px;
  border-bottom: 1px solid rgba(56,189,248,0.15);
}
.deploy-check-row {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; color: var(--text2); margin-bottom: 10px; cursor: pointer;
}
.deploy-check-row input { accent-color: var(--accent); cursor: pointer; }
#deploy-footer {
  padding: 12px 18px; border-top: 1px solid var(--border);
  display: flex; gap: 8px; align-items: center;
}
#deploy-run {
  flex: 1; padding: 9px; border-radius: 9px; border: none;
  font-size: 12px; font-weight: 700; cursor: pointer;
  background: var(--accent); color: #fff; transition: opacity 0.15s;
}
#deploy-run:hover { opacity: 0.87; }
#deploy-run:disabled { opacity: 0.4; cursor: not-allowed; }
#deploy-output {
  margin: 0 18px 14px;
  background: rgba(0,0,0,0.45); border-radius: 8px;
  padding: 10px 12px; font-family: 'SF Mono', monospace;
  font-size: 10px; line-height: 1.6; color: var(--text2);
  white-space: pre-wrap; max-height: 200px; overflow-y: auto;
  display: none; border: 1px solid var(--border);
}
#deploy-status {
  font-size: 11px; font-weight: 700; display: none;
}
#deploy-status.ok  { color: var(--green); }
#deploy-status.err { color: var(--red); }

/* ── Migrate v2 button + modal (shares the deploy look) ── */
#migrate-btn {
  margin-left: 6px;
  padding: 6px 12px; border-radius: 8px;
  font-size: 11px; font-weight: 700; cursor: pointer;
  background: rgba(168,85,247,0.14); color: #c084fc;
  border: 1px solid rgba(168,85,247,0.4);
  transition: background 0.15s, color 0.15s;
}
#migrate-btn:hover { background: rgba(168,85,247,0.22); color: #d8b4fe; border-color: #c084fc; }

#migrate-overlay {
  display: none; position: fixed; inset: 0; z-index: 8000;
  background: rgba(0,0,0,0.65); backdrop-filter: blur(4px);
  align-items: center; justify-content: center;
}
#migrate-overlay.open { display: flex; }
#migrate-modal {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 16px; overflow: hidden;
  width: 600px; max-width: 95vw; max-height: 90vh;
  display: flex; flex-direction: column;
  box-shadow: 0 24px 70px rgba(0,0,0,0.8);
}
#migrate-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
  background: linear-gradient(135deg, rgba(168,85,247,0.16) 0%, transparent 100%);
}
#migrate-bar-title { font-size: 14px; font-weight: 700; color: var(--text1); }
#migrate-close {
  background: none; border: none; color: var(--text2);
  font-size: 20px; cursor: pointer; padding: 0; line-height: 1;
}
#migrate-close:hover { color: var(--text1); }
#migrate-form { padding: 16px 18px; overflow-y: auto; flex: 1; }
#migrate-form .deploy-section-hdr { color: #c084fc; border-bottom-color: rgba(168,85,247,0.18); }

#migrate-summary {
  margin-top: 10px; padding: 10px 12px;
  background: var(--bg3); border: 1px solid var(--border); border-radius: 9px;
  font-size: 11px; line-height: 1.7; color: var(--text2);
}
#migrate-summary .ms-row { display: flex; gap: 10px; }
#migrate-summary .ms-key {
  color: var(--text2); font-weight: 700; min-width: 100px;
  text-transform: uppercase; font-size: 9.5px; letter-spacing: 0.5px;
}
#migrate-summary .ms-val {
  color: var(--text1); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all; flex: 1;
}
#migrate-summary .ms-warn { color: #facc15; }
#migrate-summary .ms-err  { color: var(--red); }
#migrate-summary .ms-ok   { color: var(--green); }
#migrate-summary .ms-empty { opacity: 0.55; }

#migrate-output {
  margin: 0 18px 14px;
  background: rgba(0,0,0,0.45); border-radius: 8px;
  padding: 10px 12px; font-family: 'SF Mono', monospace;
  font-size: 10px; line-height: 1.6; color: var(--text2);
  white-space: pre-wrap; max-height: 260px; overflow-y: auto;
  display: none; border: 1px solid var(--border);
}
#migrate-footer {
  padding: 12px 18px; border-top: 1px solid var(--border);
  display: flex; gap: 8px; align-items: center;
}
#migrate-preview-btn, #migrate-run-btn {
  padding: 9px 14px; border-radius: 9px; border: none;
  font-size: 12px; font-weight: 700; cursor: pointer;
  transition: opacity 0.15s;
}
#migrate-preview-btn {
  background: var(--bg3); color: var(--text1); border: 1px solid var(--border);
}
#migrate-preview-btn:hover { border-color: #c084fc; color: #c084fc; }
#migrate-run-btn {
  flex: 1; background: linear-gradient(135deg, #a855f7, #7e22ce); color: #fff;
}
#migrate-run-btn:hover { opacity: 0.88; }
#migrate-run-btn:disabled { opacity: 0.4; cursor: not-allowed; }
#migrate-status { font-size: 11px; font-weight: 700; display: none; }
#migrate-status.ok  { color: var(--green); }
#migrate-status.err { color: var(--red); }
#migrate-source-meta {
  font-size: 10px; color: var(--text2); margin-top: 6px;
  font-family: ui-monospace, monospace; opacity: 0.85;
}
#migrate-target-meta {
  font-size: 10px; color: var(--red); margin-top: 4px; display: none;
}
.migrate-check-list {
  display: grid; grid-template-columns: 1fr 1fr; gap: 4px 16px;
  margin-top: 6px;
}

.hdr-btn {
  font-size: 11px; color: var(--text2);
  background: none; border: 1px solid var(--border);
  border-radius: 6px; padding: 4px 11px; cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.hdr-btn:hover { color: var(--accent); border-color: var(--accent); }
.hdr-online-toggle { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text2); cursor: pointer; user-select: none; }
.hdr-online-toggle input { accent-color: var(--green); cursor: pointer; }
.hdr-online-toggle:hover { color: var(--text); }

/* ── canvas ── */
#canvas {
  flex: 1; min-height: 0;
  position: relative; overflow: auto;
  background: var(--bg);
  background-image: radial-gradient(circle at 1px 1px, rgba(56,189,248,0.06) 1px, transparent 0);
  background-size: 28px 28px;
}
/* invisible spacer that expands as cards are dragged further down/right */
#canvas-floor {
  position: absolute; top: 0; left: 0;
  width: 1px; height: 1px;
  pointer-events: none;
}

/* ── agent card ── */
.agent-card {
  position: absolute;
  width: var(--card-w);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: visible;
  cursor: grab;
  user-select: none;
  transition: box-shadow 0.15s, border-color 0.15s;
  box-shadow: 0 2px 12px rgba(0,0,0,0.5);
  display: flex;
}
.agent-card:hover { border-color: rgba(56,189,248,0.35); box-shadow: 0 4px 24px rgba(56,189,248,0.1); }
.agent-card.dragging { cursor: grabbing; box-shadow: 0 12px 40px rgba(0,0,0,0.7); z-index: 1000; border-color: var(--purple); }
.agent-card.online  { border-left: 3px solid var(--green); }
.agent-card.offline { border-left: 3px solid var(--border); }

/* master agent card */
.agent-card.master {
  border-color: rgba(188,140,255,0.3);
  background: linear-gradient(175deg, rgba(139,92,246,0.06) 0%, var(--bg2) 50%);
}
.agent-card.master:hover {
  border-color: rgba(188,140,255,0.5);
  box-shadow: 0 4px 24px rgba(139,92,246,0.2);
}
.agent-card.master.online  { border-left: 3px solid var(--purple); }
.agent-card.master.offline { border-left: 3px solid rgba(188,140,255,0.35); }
.master-crown {
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 9px; line-height: 1;
  background: rgba(139,92,246,0.2);
  border: 1px solid rgba(188,140,255,0.4);
  border-radius: 4px; padding: 1px 5px;
  margin-left: 5px; vertical-align: middle;
  color: #d2a8ff; font-weight: 700; letter-spacing: 0.5px;
}

/* dedicated row sitting just above the action button row at the bottom of
   the card. Empty when no pill, so it adds zero vertical space. */
.card-model-row {
  display: flex; align-items: center; justify-content: flex-start;
  padding: 4px 0 2px;
  min-height: 0;
}
.card-model-row:empty { display: none; }

/* model pill — shown on its own row under the domain name. Family-coloured
   so you can scan a screen of tiles and instantly spot which agents are on
   sonnet/opus vs the cheaper composer default. */
.model-pill {
  display: inline-flex; align-items: center;
  font-size: 9px; line-height: 1; font-weight: 700;
  padding: 2px 6px; border-radius: 4px;
  letter-spacing: 0.4px; text-transform: uppercase;
  font-family: 'SF Mono', 'Fira Code', monospace;
  cursor: help;
  border: 1px solid transparent;
}
.model-pill.model-composer {
  color: var(--accent);
  background: rgba(56,189,248,0.10);
  border-color: rgba(56,189,248,0.35);
}
.model-pill.model-claude {
  color: #d2a8ff;
  background: rgba(139,92,246,0.12);
  border-color: rgba(188,140,255,0.4);
}
.model-pill.model-gpt {
  color: #6ee7b7;
  background: rgba(16,185,129,0.10);
  border-color: rgba(16,185,129,0.35);
}
.model-pill.model-other {
  color: var(--text2);
  background: rgba(255,255,255,0.04);
  border-color: rgba(255,255,255,0.08);
}
#canvas.compact-view .model-pill {
  font-size: 8px; padding: 1px 4px;
}
#canvas.compact-view .card-model-row { margin-top: 1px; }

/* activity glow */
@keyframes grad-spin {
  0%   { background-position: 0% 50%; }
  50%  { background-position: 100% 50%; }
  100% { background-position: 0% 50%; }
}
@keyframes glow-fade {
  0%   { opacity: 1; }
  75%  { opacity: 0.8; }
  100% { opacity: 0; }
}
.agent-card.working { border-color: transparent !important; z-index: 1; }
.agent-card.working::before {
  content: '';
  position: absolute; inset: -3px; border-radius: 15px; z-index: -2;
  background: linear-gradient(135deg, #22c55e, #38bdf8, #a78bfa, #f97316, #38bdf8, #22c55e);
  background-size: 400% 400%;
  animation: grad-spin 3s linear infinite, glow-fade 30s ease-out forwards;
  box-shadow: 0 0 20px 3px rgba(56,189,248,0.3), 0 0 40px 6px rgba(34,197,94,0.15);
}
.agent-card.working::after {
  content: ''; position: absolute; inset: 0; border-radius: 12px; z-index: -1;
  background: var(--bg2);
}

/* card left — pane preview */
.card-left {
  flex: 1; min-width: 0;
  padding: 12px 12px 10px 14px;
  display: flex; flex-direction: column; gap: 8px;
}
.card-top { display: flex; align-items: center; gap: 8px; }
.status-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  transition: background 0.3s, box-shadow 0.3s;
}
.status-dot.online  { background: var(--green); box-shadow: 0 0 7px var(--green); }
.status-dot.offline { background: var(--text2); }
.card-name {
  font-size: 12px; font-weight: 700; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;
}
.ctx-badge {
  font-size: 9px; font-weight: 700; font-family: 'SF Mono', monospace;
  padding: 1px 5px; border-radius: 4px; flex-shrink: 0; display: none;
  border: 1px solid transparent;
}
.ctx-badge.visible { display: inline-block; }
.ctx-low  { color: var(--green);  border-color: rgba(34,197,94,0.4);  background: rgba(34,197,94,0.1); }
.ctx-mid  { color: var(--yellow); border-color: rgba(245,158,11,0.4); background: rgba(245,158,11,0.1); }
.ctx-high { color: var(--red);    border-color: rgba(239,68,68,0.4);  background: rgba(239,68,68,0.1); }

.pane-label {
  font-size: 8px; color: var(--text2); text-transform: uppercase;
  letter-spacing: 0.8px; flex-shrink: 0;
}
.pane-wrap {
  flex: 1; position: relative; min-height: 240px; max-height: 240px;
}
.pane-preview {
  width: 100%; height: 100%;
  background: rgba(0,0,0,0.35);
  border-radius: 6px;
  border: 1px solid rgba(255,255,255,0.04);
  padding: 5px 7px;
  overflow: hidden;
  display: flex; flex-direction: column; justify-content: flex-end; gap: 1px;
  box-sizing: border-box;
}
.pane-copy-btn {
  display: none; position: absolute; top: 5px; right: 6px;
  background: rgba(20,26,38,0.85); border: 1px solid rgba(255,255,255,0.12);
  border-radius: 4px; color: var(--text2); font-size: 10px;
  padding: 2px 6px; cursor: pointer; align-items: center; gap: 3px;
  backdrop-filter: blur(4px);
}
.pane-wrap:hover .pane-copy-btn { display: flex; }
.pane-copy-btn:hover { color: var(--text); border-color: rgba(255,255,255,0.3); }
.pane-copy-btn.copied { color: var(--green); }
.pane-line {
  font-size: 10px; font-family: 'SF Mono', 'Fira Code', monospace;
  color: var(--text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  line-height: 1.6;
}
.pane-line.fresh { color: var(--text); }
.pane-empty { font-size: 9px; color: rgba(107,127,153,0.35); font-style: italic; }

/* context window % badge */
.ctx-badge {
  font-size: 9px; font-weight: 700; font-family: 'SF Mono', 'Fira Code', monospace;
  padding: 1px 5px; border-radius: 4px; flex-shrink: 0; letter-spacing: 0.3px;
  border: 1px solid transparent; display: none;
}
.ctx-badge.visible { display: inline-block; }
.ctx-badge.ctx-low  { color: var(--green);  border-color: rgba(34,197,94,0.4);  background: rgba(34,197,94,0.1);  }
.ctx-badge.ctx-mid  { color: var(--yellow); border-color: rgba(245,158,11,0.4); background: rgba(245,158,11,0.1); }
.ctx-badge.ctx-high { color: var(--red);    border-color: rgba(239,68,68,0.45); background: rgba(239,68,68,0.12); }

/* card footer — action buttons */
.card-footer {
  display: flex; flex-direction: column; gap: 0;
  border-top: 1px solid rgba(255,255,255,0.04);
}
.dispatch-count {
  font-size: 10px; color: var(--accent);
  font-family: 'SF Mono', monospace; font-weight: 700;
}
/* action button row */
.card-actions {
  display: flex; align-items: stretch;
  border-top: 1px solid rgba(255,255,255,0.04);
  margin: 0 -12px -10px -14px; /* bleed flush to card-left edges */
  border-radius: 0 0 12px 0;
  overflow: hidden;
}
.card-btn {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; padding: 7px 4px;
  background: none; border: none; cursor: pointer;
  color: var(--text2); font-size: 8px; font-weight: 700;
  letter-spacing: 0.5px; text-transform: uppercase;
  border-right: 1px solid rgba(255,255,255,0.04);
  transition: color 0.15s, background 0.15s;
  flex: 1;
  -webkit-app-region: no-drag;
  border-radius: 0;
}
.card-btn:last-child { border-right: none; }
.card-btn:hover { background: rgba(255,255,255,0.06); color: var(--text); }
.card-btn svg { flex-shrink: 0; }
.btn-stop:hover     { color: var(--red);    background: rgba(239,68,68,0.08);    }
.btn-start:hover    { color: var(--green);  background: rgba(34,197,94,0.08);    }
.btn-refresh:hover  { color: var(--yellow); background: rgba(245,158,11,0.08);   }
.btn-log:hover      { color: var(--accent); background: rgba(56,189,248,0.08);   }
.btn-settings:hover { color: var(--purple); background: rgba(167,139,250,0.08);  }
.btn-restart:hover  { color: var(--green);  background: rgba(34,197,94,0.08);    }

/* card right — apps dock */
.card-right {
  display: flex; flex-direction: column;
  align-items: center; justify-content: flex-start;
  gap: 8px; padding: 12px 6px 12px;
  width: 90px; flex-shrink: 0;
  border-left: 1px solid rgba(255,255,255,0.05);
  border-radius: 0 12px 12px 0;
  background: rgba(0,0,0,0.25);
}
/* App icon style */
.app-icon {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; width: 100%;
  cursor: default;
}
.app-icon-bubble {
  width: 42px; height: 42px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  position: relative;
  transition: transform 0.15s, box-shadow 0.15s;
}
.app-icon-bubble:hover { transform: scale(1.07); }
.app-icon-label {
  font-size: 7px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--text2);
  text-align: center; line-height: 1;
}
/* RC app icon */
.app-icon-rc .app-icon-bubble {
  background: linear-gradient(135deg, #f5455c 0%, #c0392b 100%);
  box-shadow: 0 2px 12px rgba(245,69,92,0.35);
}
.app-icon-rc .app-icon-bubble svg { color: #fff; }
/* Mail app icon */
.app-icon-mail .app-icon-bubble {
  background: linear-gradient(135deg, #38bdf8 0%, #0369a1 100%);
  box-shadow: 0 2px 12px rgba(56,189,248,0.35);
}
.app-icon-mail .app-icon-bubble svg { color: #fff; }
/* status dot badge on app icon */
.app-badge {
  position: absolute; top: -3px; right: -3px;
  width: 11px; height: 11px; border-radius: 50%;
  border: 2px solid var(--bg2);
  background: var(--text2);
  transition: background 0.4s, box-shadow 0.4s;
}
.app-badge.alive      { background: var(--green);  box-shadow: 0 0 7px var(--green);  }
.app-badge.stale      { background: var(--yellow); box-shadow: 0 0 7px var(--yellow); }
.app-badge.dead       { background: var(--red);    box-shadow: 0 0 7px var(--red);    }
.app-badge.hibernated { background: #60a5fa;       box-shadow: 0 0 7px #60a5fa;       }
.app-badge.waking     { background: #fbbf24;       box-shadow: 0 0 7px #fbbf24;
                        animation: hib-wake-pulse 1.1s ease-in-out infinite; }
.app-badge.disabled   { background: #475569;       box-shadow: none; opacity: 0.85; }
@keyframes hib-wake-pulse {
  0%, 100% { opacity: 1;   box-shadow: 0 0 7px #fbbf24; }
  50%      { opacity: 0.55; box-shadow: 0 0 14px #fbbf24; }
}

/* ── action feedback toast ── */
#toast {
  position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 18px;
  font-size: 12px; color: var(--text);
  z-index: 9000; pointer-events: none;
  opacity: 0; transition: opacity 0.2s;
  white-space: nowrap;
}
#toast.show { opacity: 1; }

/* ── RC config popover ── */
#rc-popover {
  display: none; position: fixed; z-index: 6000;
  width: 300px;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 12px; overflow: hidden;
  box-shadow: 0 12px 40px rgba(0,0,0,0.7);
}
#rc-popover.open { display: block; }
#rc-pop-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px;
  background: linear-gradient(135deg, #f5455c 0%, #c0392b 100%);
}
#rc-pop-title { font-size: 12px; font-weight: 700; color: #fff; }
#rc-pop-close {
  background: none; border: none; color: rgba(255,255,255,0.8);
  font-size: 18px; cursor: pointer; line-height: 1; padding: 0;
}
#rc-pop-close:hover { color: #fff; }
#rc-pop-body { padding: 10px 12px; }
.rc-cfg-row {
  display: flex; flex-direction: column; gap: 1px;
  padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04);
}
.rc-cfg-row:last-child { border-bottom: none; }
.rc-cfg-key {
  font-size: 8px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.6px; color: var(--text2);
}
.rc-cfg-val {
  font-size: 10px; font-family: 'SF Mono', monospace;
  color: var(--text); word-break: break-all;
}
#rc-pop-actions {
  display: flex; gap: 6px; padding: 10px 12px 12px;
  border-top: 1px solid rgba(255,255,255,0.05);
}
.rc-pop-btn {
  flex: 1; padding: 7px 4px; border: none; border-radius: 7px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; cursor: pointer;
  transition: opacity 0.15s;
}
.rc-pop-btn:hover { opacity: 0.85; }
.rc-pop-btn-kill    { background: rgba(239,68,68,0.15);  color: var(--red);   border: 1px solid rgba(239,68,68,0.3); }
.rc-pop-btn-restart { background: rgba(34,197,94,0.15);  color: var(--green); border: 1px solid rgba(34,197,94,0.3); }

/* ── Mail popover ── */
#mail-popover {
  display: none; position: fixed; z-index: 6000;
  width: 300px;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 12px; overflow: hidden;
  box-shadow: 0 12px 40px rgba(0,0,0,0.7);
}
#mail-popover.open { display: block; }
#mail-pop-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px;
  background: linear-gradient(135deg, #38bdf8 0%, #0369a1 100%);
}
#mail-pop-title { font-size: 12px; font-weight: 700; color: #fff; }
#mail-pop-close {
  background: none; border: none; color: rgba(255,255,255,0.8);
  font-size: 18px; cursor: pointer; line-height: 1; padding: 0;
}
#mail-pop-close:hover { color: #fff; }
#mail-pop-body { padding: 10px 12px; }
#mail-pop-actions {
  display: flex; gap: 6px; padding: 10px 12px 12px;
  border-top: 1px solid rgba(255,255,255,0.05);
}
.mail-pop-btn {
  flex: 1; padding: 7px 4px; border: none; border-radius: 7px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; cursor: pointer;
  transition: opacity 0.15s;
}
.mail-pop-btn:hover { opacity: 0.85; }
.mail-pop-btn-test { background: rgba(56,189,248,0.15); color: var(--accent); border: 1px solid rgba(56,189,248,0.3); }
#mail-pop-output {
  margin: 0 12px 12px;
  background: rgba(0,0,0,0.35); border-radius: 6px;
  padding: 8px 10px; font-family: 'SF Mono', monospace;
  font-size: 10px; color: var(--text2); line-height: 1.5;
  white-space: pre-wrap; display: none; max-height: 120px; overflow-y: auto;
}

/* ── Browser popover ── */
#br-popover {
  display: none; position: fixed; z-index: 6000;
  width: 320px;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 12px; overflow: hidden;
  box-shadow: 0 12px 40px rgba(0,0,0,0.7);
}
#br-popover.open { display: block; }
#br-pop-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px;
  background: linear-gradient(135deg, #a78bfa 0%, #5b21b6 100%);
}
#br-pop-title { font-size: 12px; font-weight: 700; color: #fff; }
#br-pop-close {
  background: none; border: none; color: rgba(255,255,255,0.8);
  font-size: 18px; cursor: pointer; line-height: 1; padding: 0;
}
#br-pop-close:hover { color: #fff; }
#br-pop-body { padding: 10px 12px; }
#br-pop-go-row {
  display: flex; gap: 6px; padding: 0 12px 8px;
}
#br-pop-url {
  flex: 1; padding: 6px 8px; border-radius: 6px;
  background: rgba(0,0,0,0.35); border: 1px solid var(--border);
  color: var(--text); font-size: 11px;
  font-family: 'SF Mono', monospace;
}
#br-pop-url:focus { outline: none; border-color: #a78bfa; }
#br-pop-go {
  padding: 6px 12px; border-radius: 6px;
  background: rgba(167,139,250,0.18); color: #c4b5fd;
  border: 1px solid rgba(167,139,250,0.35);
  font-size: 11px; font-weight: 700; cursor: pointer;
}
#br-pop-go:hover { opacity: 0.85; }
#br-pop-actions {
  display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
  padding: 8px 12px 12px;
  border-top: 1px solid rgba(255,255,255,0.05);
}
.br-pop-btn {
  padding: 7px 4px; border: none; border-radius: 7px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.5px; cursor: pointer;
  transition: opacity 0.15s;
}
.br-pop-btn:hover { opacity: 0.85; }
.br-pop-btn-launch { background: rgba(34,197,94,0.15); color: var(--green); border: 1px solid rgba(34,197,94,0.3); }
.br-pop-btn-stop   { background: rgba(239,68,68,0.15); color: var(--red);   border: 1px solid rgba(239,68,68,0.3); }
.br-pop-btn-shot   { background: rgba(167,139,250,0.15); color: #c4b5fd;    border: 1px solid rgba(167,139,250,0.3); }
.br-pop-btn-test   { background: rgba(56,189,248,0.15); color: var(--accent); border: 1px solid rgba(56,189,248,0.3); }
#br-pop-output {
  margin: 0 12px 12px;
  background: rgba(0,0,0,0.35); border-radius: 6px;
  padding: 8px 10px; font-family: 'SF Mono', monospace;
  font-size: 10px; color: var(--text2); line-height: 1.5;
  white-space: pre-wrap; display: none; max-height: 120px; overflow-y: auto;
}
#br-pop-preview {
  display: none; width: calc(100% - 24px); margin: 0 12px 12px;
  border-radius: 6px; border: 1px solid rgba(255,255,255,0.08);
  background: #000;
}
#br-pop-preview.visible { display: block; }
.br-status-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  margin-right: 4px; vertical-align: middle;
}
.br-status-dot.running { background: var(--green); box-shadow: 0 0 6px rgba(34,197,94,0.6); }
.br-status-dot.stopped { background: #4b5563; }

/* ── Add App / App Config modal ── */
/* ── App Manager modal ── */
#appmgr-overlay {
  display: none; position: fixed; inset: 0; z-index: 7000;
  background: rgba(0,0,0,0.65); backdrop-filter: blur(6px);
  align-items: center; justify-content: center;
}
#appmgr-overlay.open { display: flex; }
#appmgr-modal {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 18px; overflow: hidden;
  width: 860px; max-width: 95vw; max-height: 85vh;
  display: flex; flex-direction: column;
  box-shadow: 0 28px 80px rgba(0,0,0,0.8), 0 0 0 1px rgba(255,255,255,0.04);
}
#appmgr-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 15px 20px; border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  background: linear-gradient(135deg, rgba(255,255,255,0.03) 0%, transparent 100%);
}
#appmgr-title { font-size: 14px; font-weight: 800; color: var(--text1); }
#appmgr-close {
  background: rgba(255,255,255,0.06); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text2); font-size: 16px;
  cursor: pointer; line-height: 1; padding: 4px 9px;
  transition: background 0.15s, color 0.15s;
}
#appmgr-close:hover { background: rgba(255,255,255,0.12); color: var(--text1); }

/* two-panel layout */
#appmgr-inner {
  display: flex; flex: 1; min-height: 0; overflow: hidden;
}
/* left sidebar — app list */
#appmgr-sidebar {
  width: 220px; flex-shrink: 0;
  border-right: 1px solid var(--border);
  overflow-y: auto; padding: 12px 10px;
  display: flex; flex-direction: column; gap: 4px;
  background: rgba(0,0,0,0.15);
}
#appmgr-sidebar-label {
  font-size: 9px; font-weight: 800; letter-spacing: 1.2px; text-transform: uppercase;
  color: var(--text2); padding: 4px 8px 8px; opacity: 0.6;
}
.appmgr-app-row {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 10px; border-radius: 10px; cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.15s, border-color 0.15s;
  position: relative;
}
.appmgr-app-row:hover { background: rgba(255,255,255,0.05); }
.appmgr-app-row.active {
  background: rgba(255,255,255,0.07);
  border-color: rgba(255,255,255,0.1);
}
.appmgr-app-dot {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
}
.appmgr-app-name {
  font-size: 12px; font-weight: 600; color: var(--text); flex: 1;
}
.appmgr-app-badge {
  font-size: 9px; font-weight: 700; padding: 2px 6px; border-radius: 8px;
  background: rgba(34,197,94,0.15); color: #4ade80;
  border: 1px solid rgba(34,197,94,0.25);
}

/* right detail panel */
#appmgr-detail {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
}
#appmgr-detail-header {
  padding: 16px 20px 12px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#appmgr-detail-title {
  font-size: 15px; font-weight: 800; color: var(--text1); margin-bottom: 3px;
}
#appmgr-detail-desc {
  font-size: 11px; color: var(--text2);
}
#appmgr-body {
  flex: 1; overflow-y: auto; padding: 18px 20px;
}
/* fields */
#appmgr-fields { display: flex; flex-direction: column; gap: 12px; }
.appmgr-field { display: flex; flex-direction: column; gap: 5px; }
.appmgr-field label {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.6px; color: var(--text2);
}
.appmgr-field input {
  background: rgba(0,0,0,0.3); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text1); font-size: 12px;
  padding: 9px 12px; outline: none; width: 100%;
  transition: border-color 0.15s;
}
.appmgr-field input:focus { border-color: var(--accent); }
#appmgr-footer {
  display: flex; align-items: center; gap: 8px;
  padding: 14px 20px; border-top: 1px solid var(--border); flex-shrink: 0;
}
#appmgr-save {
  padding: 9px 22px; border-radius: 9px; border: none;
  font-size: 12px; font-weight: 700; cursor: pointer;
  background: var(--accent); color: #000;
  transition: opacity 0.15s;
}
#appmgr-save:hover { opacity: 0.87; }
#appmgr-save:disabled { opacity: 0.4; cursor: not-allowed; }
#appmgr-delete {
  padding: 9px 16px; border-radius: 9px;
  font-size: 12px; font-weight: 700; cursor: pointer;
  background: rgba(239,68,68,0.1); color: var(--red);
  border: 1px solid rgba(239,68,68,0.25);
  transition: opacity 0.15s;
  display: none;
}
#appmgr-delete.visible { display: block; }
#appmgr-delete:hover { opacity: 0.8; }
#appmgr-msg {
  font-size: 11px; color: var(--green);
  display: none; align-self: center;
}
#appmgr-msg.err { color: var(--red); }
/* add-app button in the dock */
.app-add-btn {
  width: 34px; height: 34px; border-radius: 9px; border: none;
  background: rgba(255,255,255,0.06); color: var(--text2);
  font-size: 20px; line-height: 1; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s, color 0.15s;
  margin-top: 4px;
}
.app-add-btn:hover { background: rgba(255,255,255,0.12); color: var(--text1); }

/* ── Agent info button ── */
.card-info-btn {
  background: none; border: none; cursor: pointer;
  color: var(--text2); line-height: 1;
  padding: 2px; border-radius: 4px;
  transition: color 0.15s, transform 0.2s;
  flex-shrink: 0; display: flex; align-items: center;
}
.card-info-btn:hover { color: var(--accent); transform: rotate(45deg); }

/* ── Agent info panel ── */
#agent-info-overlay {
  display: none; position: fixed; inset: 0; z-index: 7500;
  background: rgba(0,0,0,0.6); backdrop-filter: blur(6px);
  align-items: center; justify-content: center;
}
#agent-info-overlay.open { display: flex; }
#agent-info-panel {
  width: 1100px; max-width: 94vw; max-height: 84vh;
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 18px; display: flex; flex-direction: column;
  box-shadow: 0 32px 90px rgba(0,0,0,0.85), 0 0 0 1px rgba(255,255,255,0.04);
  transform: scale(0.94) translateY(12px);
  opacity: 0;
  transition: transform 0.2s cubic-bezier(0.34,1.56,0.64,1), opacity 0.18s ease;
}
#agent-info-overlay.open #agent-info-panel {
  transform: scale(1) translateY(0);
  opacity: 1;
}
#agent-info-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px 0; flex-shrink: 0;
}
#agent-info-title {
  font-size: 15px; font-weight: 800; color: var(--text1);
  background: linear-gradient(90deg, var(--text1), var(--text2));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
#agent-info-close {
  background: rgba(255,255,255,0.06); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text2); font-size: 16px;
  cursor: pointer; line-height: 1; padding: 4px 9px;
  transition: background 0.15s, color 0.15s;
}
#agent-info-close:hover { background: rgba(255,255,255,0.12); color: var(--text1); }
#agent-info-edit {
  display: none; align-items: center; gap: 5px;
  background: rgba(255,255,255,0.06); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text2); font-size: 12px;
  cursor: pointer; padding: 4px 10px;
  transition: background 0.15s, color 0.15s;
}
#agent-info-edit:hover { background: rgba(255,255,255,0.12); color: var(--text1); }
#agent-info-edit.editing { color: var(--green); border-color: rgba(63,185,80,0.4); background: rgba(63,185,80,0.08); }
#context-editor {
  width: 100%; height: 100%; min-height: 420px;
  background: rgba(0,0,0,0.3); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); font-family: 'SF Mono','Fira Code',monospace;
  font-size: 12px; line-height: 1.6; padding: 14px 16px; box-sizing: border-box;
  resize: vertical; outline: none;
}
#context-editor:focus { border-color: rgba(88,166,255,0.4); }
#context-edit-actions {
  display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-shrink: 0;
}
#context-save-btn {
  background: rgba(63,185,80,0.15); border: 1px solid rgba(63,185,80,0.4);
  border-radius: 7px; color: var(--green); font-size: 12px;
  padding: 5px 16px; cursor: pointer;
}
#context-save-btn:hover { background: rgba(63,185,80,0.25); }
#context-cancel-btn {
  background: none; border: none; color: var(--text2); font-size: 12px;
  cursor: pointer; padding: 5px 4px;
}
#context-cancel-btn:hover { color: var(--text); }
#context-edit-status { font-size: 11px; color: var(--text2); margin-left: 4px; }
#agent-info-tabs {
  display: flex; gap: 0; padding: 12px 20px 0; flex-shrink: 0;
  border-bottom: 1px solid var(--border); margin-top: 4px;
}
.agent-info-tab {
  padding: 8px 16px; font-size: 11px; font-weight: 700;
  cursor: pointer; border: none; background: none;
  color: var(--text2); border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
  margin-bottom: -1px; border-radius: 6px 6px 0 0;
}
.agent-info-tab:hover { color: var(--text1); background: rgba(255,255,255,0.04); }
.agent-info-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
#agent-info-body {
  flex: 1; min-height: 0; overflow-y: auto; padding: 20px 22px;
}
#agent-info-body h1 { font-size: 16px; margin: 0 0 14px; color: var(--text1); }
#agent-info-body h2 { font-size: 13px; margin: 18px 0 8px; color: var(--text1); }
#agent-info-body h3 { font-size: 12px; margin: 14px 0 6px; color: var(--text2); }
#agent-info-body p  { font-size: 12px; line-height: 1.7; color: var(--text2); margin: 0 0 10px; }
#agent-info-body pre {
  background: rgba(0,0,0,0.35); border-radius: 7px;
  padding: 10px 12px; font-size: 10px; line-height: 1.6;
  overflow-x: auto; margin: 0 0 12px;
}
#agent-info-body code { font-family: 'SF Mono', monospace; font-size: 10px; }
#agent-info-body table { width: 100%; border-collapse: collapse; font-size: 11px; margin-bottom: 12px; }
#agent-info-body th { text-align: left; color: var(--text2); font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.5px; padding: 4px 8px;
  border-bottom: 1px solid var(--border); }
#agent-info-body td { padding: 5px 8px; color: var(--text2); border-bottom: 1px solid rgba(255,255,255,0.04); }
#agent-info-body ul, #agent-info-body ol { padding-left: 18px; margin: 0 0 10px; }
#agent-info-body li { font-size: 12px; line-height: 1.7; color: var(--text2); }
#agent-info-body a { color: var(--accent); text-decoration: none; }
.info-file-list { display: flex; flex-direction: column; gap: 6px; }
.info-file-item {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border-radius: 8px;
  background: var(--bg3); cursor: pointer;
  border: 1px solid var(--border); transition: border-color 0.15s;
}
.info-file-item:hover { border-color: var(--accent); }
.info-file-name { font-size: 11px; font-weight: 600; color: var(--text1); }
.info-file-size { font-size: 10px; color: var(--text2); margin-left: auto; }
#agent-info-loading {
  text-align: center; padding: 40px; font-size: 12px;
  color: var(--text2); opacity: 0.4;
}

/* ── file manager tab ── */
#fm-wrap { display: flex; flex-direction: column; gap: 0; flex: 1; min-height: 0; overflow: hidden; }
#fm-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 0 10px; flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
#fm-upload-btn {
  display: flex; align-items: center; gap: 5px;
  padding: 5px 12px; border-radius: 7px; font-size: 11px; font-weight: 700;
  background: rgba(56,189,248,0.12); color: var(--accent);
  border: 1px solid rgba(56,189,248,0.3); cursor: pointer;
  transition: background 0.15s;
}
#fm-upload-btn:hover { background: rgba(56,189,248,0.2); }
#fm-upload-input { display: none; }
#fm-path-crumb {
  font-size: 11px; color: var(--text2);
  flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
#fm-list {
  flex: 1; min-height: 0; overflow-y: auto; padding: 10px 0;
  display: flex; flex-direction: column; gap: 2px;
}
.fm-row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: 7px;
  border: 1px solid transparent;
  cursor: pointer; transition: background 0.12s, border-color 0.12s;
}
.fm-row:hover { background: var(--bg3); border-color: var(--border); }
.fm-row-icon { font-size: 14px; flex-shrink: 0; width: 20px; text-align: center; }
.fm-row-name { font-size: 11px; font-weight: 600; color: var(--text); flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fm-row-size { font-size: 10px; color: var(--text2); flex-shrink: 0; }
.fm-row-actions { display: flex; gap: 4px; flex-shrink: 0; opacity: 0; transition: opacity 0.12s; }
.fm-row:hover .fm-row-actions { opacity: 1; }
.fm-act-btn {
  padding: 2px 7px; border-radius: 5px; font-size: 9px; font-weight: 700;
  border: 1px solid var(--border); background: var(--bg2);
  color: var(--text2); cursor: pointer; transition: color 0.12s, border-color 0.12s;
}
.fm-act-btn:hover { color: var(--accent); border-color: var(--accent); }
.fm-act-dl:hover  { color: var(--green);  border-color: var(--green);  }
.fm-empty { font-size: 11px; color: var(--text2); opacity: 0.4; padding: 20px 0; text-align: center; }
#fm-viewer {
  position: absolute; inset: 0; background: var(--bg2);
  display: flex; flex-direction: column; border-radius: 12px; overflow: hidden;
  z-index: 10;
}
#fm-viewer-bar {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#fm-viewer-name { font-size: 12px; font-weight: 700; color: var(--text); flex: 1; }
#fm-viewer-dl {
  padding: 4px 10px; border-radius: 6px; font-size: 10px; font-weight: 700;
  background: rgba(34,197,94,0.12); color: var(--green);
  border: 1px solid rgba(34,197,94,0.3); cursor: pointer;
}
#fm-viewer-close {
  padding: 4px 9px; border-radius: 6px; font-size: 14px; font-weight: 400;
  background: rgba(255,255,255,0.06); color: var(--text2);
  border: 1px solid var(--border); cursor: pointer;
}
#fm-viewer-body { flex: 1; overflow-y: auto; padding: 14px 16px; font-size: 11px; line-height: 1.7; }

/* ── logs tab ── */
#logs-wrap {
  display: flex; flex-direction: column; flex: 1; min-height: 0;
  overflow: hidden; padding: 12px 0 8px;
}
#logs-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding-bottom: 10px; flex-shrink: 0; flex-wrap: wrap;
}
.logs-file-pill {
  padding: 4px 11px; border-radius: 999px; font-size: 11px; font-weight: 600;
  border: 1px solid var(--border); background: var(--bg2);
  color: var(--text2); cursor: pointer; transition: all 0.12s;
  display: inline-flex; align-items: center; gap: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.logs-file-pill:hover { color: var(--text); border-color: var(--accent); }
.logs-file-pill.active {
  background: rgba(56,189,248,0.15); color: var(--accent); border-color: var(--accent);
}
.logs-file-pill .logs-file-size { font-size: 9.5px; opacity: 0.65; font-family: inherit; }
#logs-toolbar-spacer { flex: 1; min-width: 8px; }
#logs-lines-select, .logs-mini-btn {
  padding: 4px 9px; border-radius: 6px; font-size: 10.5px; font-weight: 600;
  background: var(--bg2); color: var(--text2);
  border: 1px solid var(--border); cursor: pointer;
}
#logs-lines-select:focus { outline: none; border-color: var(--accent); }
.logs-mini-btn:hover { color: var(--text); border-color: var(--accent); }
.logs-mini-btn.on {
  background: rgba(34,197,94,0.14); color: var(--green);
  border-color: rgba(34,197,94,0.4);
}
#logs-meta {
  font-size: 10px; color: var(--text2); padding: 0 4px 6px;
  display: flex; gap: 12px; flex-wrap: wrap; flex-shrink: 0;
}
#logs-meta b { color: var(--text); font-weight: 600; }
#logs-view {
  flex: 1; min-height: 0; overflow-y: auto;
  background: #0a0e14; color: #c9d1d9;
  border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-word;
}
#logs-view .ll-warn  { color: #facc15; }
#logs-view .ll-err   { color: #f87171; }
#logs-view .ll-ok    { color: #4ade80; }
#logs-view .ll-dim   { color: #6b7280; }
.logs-empty {
  font-size: 11px; color: var(--text2); opacity: 0.6;
  padding: 24px 4px; text-align: center;
}

/* ── git tab ── */
#git-wrap {
  display: flex; flex-direction: column; flex: 1; min-height: 0;
  overflow-y: auto; padding: 12px 0 14px; gap: 14px;
}
.git-card {
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg3); padding: 10px 12px;
}
.git-card-title {
  font-size: 10px; font-weight: 700; color: var(--text2);
  text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 8px;
}
.git-meta-row {
  display: flex; gap: 14px; font-size: 11px; color: var(--text2);
  flex-wrap: wrap;
}
.git-meta-row b { color: var(--text); font-weight: 600; }
.git-meta-row code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px; color: var(--accent);
  background: rgba(56,189,248,0.08); padding: 1px 5px; border-radius: 4px;
}
.git-status-pill {
  display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.4px;
}
.git-status-clean { background: rgba(34,197,94,0.12); color: var(--green); border: 1px solid rgba(34,197,94,0.35); }
.git-status-dirty { background: rgba(250,204,21,0.12); color: #facc15; border: 1px solid rgba(250,204,21,0.4); }
.git-status-error { background: rgba(248,113,113,0.12); color: #f87171; border: 1px solid rgba(248,113,113,0.4); }
.git-changes-list {
  margin-top: 8px; max-height: 160px; overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px; line-height: 1.6;
}
.git-change-row {
  display: flex; gap: 8px; padding: 2px 4px; border-radius: 4px;
}
.git-change-row:hover { background: rgba(255,255,255,0.03); }
.git-change-flag {
  flex-shrink: 0; width: 28px; font-weight: 700; color: var(--text2);
}
.git-change-flag.M  { color: #facc15; }
.git-change-flag.A  { color: var(--green); }
.git-change-flag.D  { color: #f87171; }
.git-change-flag.R  { color: var(--accent); }
.git-change-flag.U  { color: #f87171; }
.git-change-flag.QQ { color: var(--text2); } /* untracked ?? */
.git-change-name { color: var(--text); flex: 1; word-break: break-all; }
.git-commit-list {
  margin-top: 4px; display: flex; flex-direction: column; gap: 4px;
}
.git-commit-row {
  display: flex; gap: 10px; padding: 4px 6px; border-radius: 5px;
  font-size: 10.5px;
}
.git-commit-row:hover { background: rgba(255,255,255,0.03); }
.git-commit-sha {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--accent); flex-shrink: 0; width: 60px;
}
.git-commit-subj { color: var(--text); flex: 1; word-break: break-word; }
.git-commit-meta { color: var(--text2); flex-shrink: 0; font-size: 10px; }
#git-commit-msg {
  width: 100%; box-sizing: border-box;
  background: var(--bg2); color: var(--text);
  border: 1px solid var(--border); border-radius: 7px;
  padding: 7px 10px; font-size: 11px; font-family: inherit;
  resize: vertical; min-height: 56px;
}
#git-commit-msg:focus { outline: none; border-color: var(--accent); }
.git-actions {
  display: flex; gap: 8px; margin-top: 8px; align-items: center;
  flex-wrap: wrap;
}
.git-btn {
  padding: 6px 14px; border-radius: 7px; font-size: 11px; font-weight: 700;
  border: 1px solid var(--border); background: var(--bg2);
  color: var(--text2); cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
  display: inline-flex; align-items: center; gap: 5px;
}
.git-btn:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
.git-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.git-btn-primary {
  background: rgba(34,197,94,0.14); color: var(--green);
  border-color: rgba(34,197,94,0.4);
}
.git-btn-primary:hover:not(:disabled) {
  background: rgba(34,197,94,0.22); color: var(--green); border-color: var(--green);
}
.git-output {
  margin-top: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px; line-height: 1.55;
  background: #0a0e14; color: #c9d1d9;
  border: 1px solid var(--border); border-radius: 7px;
  padding: 8px 11px; max-height: 200px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-all;
}
.git-empty {
  font-size: 11px; color: var(--text2); opacity: 0.7;
  padding: 16px 4px; text-align: center;
}
#git-manual-path {
  background: var(--bg2); color: var(--text);
  border: 1px solid var(--border); border-radius: 6px;
  padding: 5px 9px; font-size: 11px; font-family: ui-monospace, monospace;
  width: 240px;
}
#git-manual-path:focus { outline: none; border-color: var(--accent); }

/* ── xterm terminal modal ── */
#term-overlay {
  display: none; position: fixed; inset: 0; z-index: 5000;
  background: rgba(0,0,0,0.75); backdrop-filter: blur(6px);
  align-items: center; justify-content: center;
}
#term-overlay.open { display: flex; }
#term-modal {
  width: min(960px, 92vw); height: 78vh;
  background: #0d1117; border: 1px solid var(--border);
  border-radius: 14px; overflow: hidden;
  display: flex; flex-direction: column;
  box-shadow: 0 24px 80px rgba(0,0,0,0.9);
}
#term-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 14px;
  background: rgba(0,0,0,0.5); border-bottom: 1px solid rgba(255,255,255,0.06);
  flex-shrink: 0;
}
#term-title { font-size: 13px; font-weight: 700; color: var(--purple); }
#term-status { font-size: 10px; color: var(--text2); }
#term-close {
  background: none; border: none; color: var(--text2);
  font-size: 20px; cursor: pointer; line-height: 1;
}
#term-close:hover { color: var(--text); }
#term-copy:hover { color: var(--text); border-color: rgba(255,255,255,0.3); }
#term-copy.copied { color: var(--green); border-color: rgba(63,185,80,0.4); }
#term-container {
  flex: 1; min-height: 0; padding: 6px 8px;
  background: #0d1117;
}
/* xterm.js overrides */
.xterm { height: 100% !important; }
.xterm-viewport { scrollbar-width: thin; scrollbar-color: rgba(100,120,160,0.3) transparent; }

/* ── hint ── */
#map-hint {
  position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%);
  font-size: 10px; color: var(--text2);
  background: rgba(10,14,23,0.8); padding: 4px 14px; border-radius: 20px;
  pointer-events: none; border: 1px solid var(--border);
}

/* ── browser panel (docs / apps / modules) ── */
#browser-panel {
  display: none; position: fixed;
  top: 46px; left: 0; right: 0; bottom: 0;
  background: var(--bg); z-index: 4000;
  flex-direction: row;
}
#browser-panel.open { display: flex; }

#browser-tree {
  width: 320px; flex-shrink: 0;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  overflow-y: auto; padding: 12px 8px;
  display: flex; flex-direction: column; gap: 2px;
}
.tree-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: 6px;
  font-size: 11px; color: var(--text2);
  cursor: pointer; user-select: none;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  transition: background 0.12s, color 0.12s;
}
.tree-item:hover { background: rgba(255,255,255,0.05); color: var(--text); }
.tree-item.active { background: rgba(56,189,248,0.12); color: var(--accent); }
.tree-item-icon { font-size: 13px; flex-shrink: 0; }
.tree-section {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--text2); opacity: 0.5;
  padding: 10px 10px 4px; pointer-events: none;
}
/* folder row */
.tree-folder {
  display: flex; align-items: center; gap: 7px;
  padding: 6px 10px; border-radius: 6px;
  font-size: 11px; font-weight: 700; color: var(--text);
  cursor: pointer; user-select: none;
  transition: background 0.12s;
}
.tree-folder:hover { background: rgba(255,255,255,0.05); }
.tree-folder-arrow {
  font-size: 9px; color: var(--text2);
  transition: transform 0.15s; display: inline-block;
}
.tree-folder.open .tree-folder-arrow { transform: rotate(90deg); }
.tree-folder-children {
  display: none; padding-left: 14px;
}
.tree-folder-children.open { display: block; }
/* archive/restore action buttons in tree */
.tree-action-btn {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.4px; padding: 2px 6px; border-radius: 4px;
  border: 1px solid var(--border); cursor: pointer;
  background: var(--bg3); flex-shrink: 0;
  opacity: 0; transition: opacity 0.15s;
  margin-left: auto;
}
.tree-folder:hover .tree-action-btn { opacity: 1; }
.tree-archive-btn { color: var(--yellow); border-color: rgba(245,158,11,0.3); }
.tree-archive-btn:hover { background: rgba(245,158,11,0.1); }
.tree-restore-btn { color: var(--green); border-color: rgba(34,197,94,0.3); }
.tree-restore-btn:hover { background: rgba(34,197,94,0.1); }
.tree-start-btn   { color: var(--green); border-color: rgba(34,197,94,0.3); }
.tree-start-btn:hover   { background: rgba(34,197,94,0.1); }
.tree-stop-btn    { color: var(--red);   border-color: rgba(239,68,68,0.3); }
.tree-stop-btn:hover    { background: rgba(239,68,68,0.1); }
.tree-action-btn:disabled { opacity: 0.5; cursor: progress; }
/* indent child items */
.tree-folder-children .tree-item { padding-left: 10px; }

#browser-content {
  flex: 1; min-width: 0;
  display: flex; flex-direction: column;
  overflow: hidden;
}
#browser-toolbar {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 18px; border-bottom: 1px solid var(--border);
  background: var(--bg2); flex-shrink: 0;
}
#browser-file-path {
  font-size: 11px; font-family: 'SF Mono', monospace;
  color: var(--text2); flex: 1;
}
#browser-close {
  background: none; border: none; color: var(--text2);
  font-size: 20px; cursor: pointer; line-height: 1;
}
#browser-close:hover { color: var(--text); }
#browser-body {
  flex: 1; overflow-y: auto; padding: 24px 32px;
  scrollbar-width: thin; scrollbar-color: rgba(100,120,160,0.3) transparent;
}

/* markdown rendering */
#browser-body h1 { font-size: 22px; font-weight: 700; margin: 0 0 16px; color: var(--text); border-bottom: 1px solid var(--border); padding-bottom: 10px; }
#browser-body h2 { font-size: 17px; font-weight: 700; margin: 28px 0 10px; color: var(--text); }
#browser-body h3 { font-size: 14px; font-weight: 700; margin: 20px 0 8px; color: var(--text); }
#browser-body p  { font-size: 13px; line-height: 1.75; color: var(--text2); margin: 0 0 12px; }
#browser-body ul, #browser-body ol { padding-left: 20px; margin: 0 0 12px; }
#browser-body li { font-size: 13px; line-height: 1.7; color: var(--text2); }
#browser-body a  { color: var(--accent); text-decoration: none; }
#browser-body a:hover { text-decoration: underline; }
#browser-body code { font-family: 'SF Mono', monospace; font-size: 11px; background: rgba(255,255,255,0.07); padding: 1px 5px; border-radius: 4px; color: var(--purple); }
#browser-body pre { background: rgba(0,0,0,0.4); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; overflow-x: auto; margin: 0 0 14px; }
#browser-body pre code { background: none; padding: 0; color: #c9d1d9; font-size: 12px; }
#browser-body table { width: 100%; border-collapse: collapse; margin: 0 0 16px; font-size: 12px; }
#browser-body th { text-align: left; padding: 7px 12px; background: rgba(255,255,255,0.04); color: var(--text); font-weight: 700; border-bottom: 1px solid var(--border); }
#browser-body td { padding: 6px 12px; color: var(--text2); border-bottom: 1px solid rgba(255,255,255,0.04); }
#browser-body blockquote { border-left: 3px solid var(--accent); padding: 4px 14px; margin: 0 0 12px; color: var(--text2); background: rgba(56,189,248,0.05); border-radius: 0 6px 6px 0; }
#browser-body hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
/* plain code view */
#browser-body pre.code-plain { background: rgba(0,0,0,0.5); }

/* ── Floating Rocket.Chat feed (global message viewer) ───────────────── */
#rc-feed {
  position: fixed; bottom: 20px; right: 20px;
  width: 340px; max-height: 70vh;
  z-index: 200;
  background: rgba(10, 14, 23, 0.78);
  backdrop-filter: blur(24px) saturate(1.2);
  -webkit-backdrop-filter: blur(24px) saturate(1.2);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.6);
  display: flex; flex-direction: column; overflow: hidden;
  transition: opacity 0.2s, transform 0.2s, border-color 0.2s, box-shadow 0.2s;
}
#rc-feed.hidden { opacity: 0; pointer-events: none; transform: translateY(8px); }
#rc-feed.has-unread {
  border-color: var(--purple);
  box-shadow: 0 0 0 1px var(--purple), 0 12px 36px rgba(0,0,0,0.6);
}

#rc-feed-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px;
  background: rgba(255,255,255,0.03);
  border-bottom: 1px solid var(--border);
  cursor: grab; user-select: none; flex-shrink: 0;
}
#rc-feed-bar:active { cursor: grabbing; }
#rc-feed-title {
  display: flex; align-items: center; gap: 7px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
  text-transform: uppercase; color: var(--accent);
}
#rc-feed-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--accent); flex-shrink: 0;
  box-shadow: 0 0 6px var(--accent);
  transition: background 0.15s, box-shadow 0.15s;
}
#rc-feed-dot.loading { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
#rc-feed-dot.error   { background: var(--red);    box-shadow: 0 0 6px var(--red); }
#rc-feed-dot.ok      { background: var(--green);  box-shadow: 0 0 6px var(--green); }
#rc-feed-actions { display: flex; align-items: center; gap: 2px; }
#rc-feed-actions button {
  background: none; border: none; color: var(--text2); cursor: pointer;
  padding: 3px 5px; line-height: 1; border-radius: 4px;
  transition: color 0.1s, background 0.1s;
  display: inline-flex; align-items: center;
}
#rc-feed-actions button:hover { color: var(--accent); background: rgba(56,189,248,0.08); }

#rc-feed-body {
  flex: 1; min-height: 0;
  overflow-y: auto; overflow-x: hidden;
  padding: 2px 0;
  scrollbar-width: thin; scrollbar-color: rgba(167,139,250,0.4) transparent;
}
#rc-feed-body::-webkit-scrollbar { width: 4px; }
#rc-feed-body::-webkit-scrollbar-thumb { background: rgba(167,139,250,0.4); border-radius: 4px; }

.rc-row {
  padding: 8px 12px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
  cursor: pointer;
  transition: background 0.1s;
}
.rc-row:last-child { border-bottom: none; }
.rc-row:hover { background: rgba(56,189,248,0.07); }
.rc-row.rc-mine { background: rgba(167,139,250,0.04); }
.rc-row.rc-orphan { cursor: default; }
.rc-row-room {
  display: flex; align-items: center; gap: 5px;
  font-size: 9px; font-weight: 700; letter-spacing: 0.4px;
  text-transform: uppercase; color: var(--accent);
  margin-bottom: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rc-row.rc-orphan .rc-row-room { color: var(--text2); }
.rc-row-room-icon { font-size: 8px; opacity: 0.7; flex-shrink: 0; }
.rc-row-meta { display: flex; gap: 6px; align-items: baseline; margin-bottom: 2px; }
.rc-row-user { font-size: 11px; font-weight: 600; color: var(--text); }
.rc-row.rc-mine .rc-row-user { color: var(--purple); }
.rc-row-time { font-size: 10px; color: var(--text2); }
.rc-row-text {
  font-size: 11px; color: var(--text2); line-height: 1.45;
  word-break: break-word;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden;
}
.rc-row.rc-mine .rc-row-text { font-style: italic; opacity: 0.65; }

#rc-feed-status {
  font-size: 10px; color: var(--text2); text-align: center;
  padding: 7px 10px;
  border-top: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  flex-shrink: 0;
}

/* the FAB shown when the panel is hidden */
#rc-feed-toggle {
  position: fixed; bottom: 20px; right: 20px; z-index: 200;
  width: 44px; height: 44px; border-radius: 50%;
  background: rgba(10, 14, 23, 0.85);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--accent);
  color: var(--accent); cursor: pointer;
  display: none; align-items: center; justify-content: center;
  box-shadow: 0 4px 18px rgba(0,0,0,0.6);
  transition: transform 0.15s, box-shadow 0.15s;
}
#rc-feed-toggle.visible { display: flex; }
#rc-feed-toggle:hover {
  transform: scale(1.08);
  box-shadow: 0 0 0 2px var(--accent), 0 4px 18px rgba(0,0,0,0.6);
}
#rc-feed-toggle.has-unread {
  border-color: var(--purple);
  color: var(--purple);
}
#rc-feed-toggle.has-unread::after {
  content: ''; position: absolute; top: 3px; right: 3px;
  width: 11px; height: 11px; border-radius: 50%;
  background: var(--purple);
  box-shadow: 0 0 8px var(--purple), 0 0 0 2px var(--bg);
  animation: rc-feed-pulse 2s ease-in-out infinite;
}
@keyframes rc-feed-pulse {
  0%, 100% { transform: scale(1);    opacity: 1; }
  50%      { transform: scale(1.25); opacity: 0.85; }
}

/* responsive: collapse to FAB on narrow screens */
@media (max-width: 600px) {
  #rc-feed { width: calc(100vw - 24px); right: 12px; bottom: 12px; }
}

/* ── Quick Task dialog ───────────────────────────────────────────────── */
/* FAB sits to the LEFT of #rc-feed-toggle (right:72px = 20px + 44px + 8px
   gap). z-index 201 keeps it above the rc-feed panel so it stays clickable
   even when the rc panel is showing. */
#task-dialog-toggle {
  position: fixed; bottom: 20px; right: 72px; z-index: 201;
  width: 44px; height: 44px; border-radius: 50%;
  background: rgba(10, 14, 23, 0.85);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--purple);
  color: var(--purple); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 18px rgba(0,0,0,0.6);
  transition: transform 0.15s, box-shadow 0.15s;
}
#task-dialog-toggle:hover {
  transform: scale(1.08);
  box-shadow: 0 0 0 2px var(--purple), 0 4px 18px rgba(0,0,0,0.6);
}
#task-dialog-toggle.dialog-open { opacity: 0; pointer-events: none; }

/* The dialog defaults to bottom-right but offset to the LEFT of the
   rc-feed panel area so both can sit open side-by-side without overlap.
   right: 380px ≈ 20px (rc-feed right) + 340px (rc-feed width) + 20px gap. */
#task-dialog {
  position: fixed; bottom: 20px; right: 380px;
  width: 360px; z-index: 202;
  background: rgba(10, 14, 23, 0.82);
  backdrop-filter: blur(24px) saturate(1.2);
  -webkit-backdrop-filter: blur(24px) saturate(1.2);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.65);
  display: flex; flex-direction: column; overflow: hidden;
  transition: opacity 0.2s, transform 0.2s, border-color 0.2s, box-shadow 0.2s;
}
#task-dialog.hidden { opacity: 0; pointer-events: none; transform: translateY(8px); }
#task-dialog-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px;
  background: rgba(255,255,255,0.03);
  border-bottom: 1px solid var(--border);
  cursor: grab; user-select: none; flex-shrink: 0;
}
#task-dialog-bar:active { cursor: grabbing; }
#task-dialog-title {
  display: flex; align-items: center; gap: 7px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
  text-transform: uppercase; color: var(--purple);
}
#task-dialog-actions { display: flex; align-items: center; gap: 2px; }
#task-dialog-actions button {
  background: none; border: none; color: var(--text2); cursor: pointer;
  padding: 3px 5px; line-height: 1; border-radius: 4px;
  transition: color 0.1s, background 0.1s;
  display: inline-flex; align-items: center;
  font-size: 18px; font-weight: 600;
}
#task-dialog-actions button:hover { color: var(--purple); background: rgba(167,139,250,0.10); }

#task-dialog-body {
  padding: 12px; display: flex; flex-direction: column; gap: 9px;
  flex-shrink: 0;
}
#task-dialog-body label {
  font-size: 10px; font-weight: 600; letter-spacing: 0.4px;
  text-transform: uppercase; color: var(--text2);
}
#task-agent {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 10px;
  color: var(--text); font-size: 12px;
  outline: none; cursor: pointer;
  width: 100%;
}
#task-agent:focus { border-color: var(--purple); }
#task-agent option.offline { color: var(--text2); }
#task-text {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 9px 11px;
  color: var(--text); font-size: 12px; line-height: 1.5;
  font-family: inherit;
  outline: none; resize: none;
  min-height: 80px; max-height: 280px;
  width: 100%; box-sizing: border-box;
}
#task-text::placeholder { color: var(--text2); opacity: 0.55; }
#task-text:focus { border-color: var(--purple); }

#task-dialog-footer {
  display: flex; align-items: center; gap: 9px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  flex-shrink: 0;
}
#task-delegate-btn {
  font-size: 12px; font-weight: 700;
  padding: 8px 16px; border-radius: 7px;
  background: var(--purple); color: #0a0e17;
  border: none; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: filter 0.15s, transform 0.05s;
}
#task-delegate-btn:hover  { filter: brightness(1.1); }
#task-delegate-btn:active { transform: translateY(1px); }
#task-delegate-btn:disabled {
  opacity: 0.5; cursor: not-allowed; filter: grayscale(0.4);
}
#task-dialog-status {
  font-size: 10px; color: var(--text2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  flex: 1; min-width: 0;
}
#task-dialog-status.ok    { color: var(--green); }
#task-dialog-status.error { color: var(--red); }
#task-dialog-hint {
  font-size: 9px; color: var(--text2); opacity: 0.55;
  font-family: 'SF Mono', monospace;
}

/* responsive: stack below rc-feed on narrow screens */
@media (max-width: 800px) {
  #task-dialog { right: 12px; left: 12px; width: auto; bottom: 12px; }
  #task-dialog-toggle { right: 72px; }
}

/* ── Task Planner ─────────────────────────────────────────────────────
   The third FAB sits LEFT of #task-dialog-toggle (which is at right:72px).
   72 + 44 + 8 = 124. Amber accent so it reads as "your" workspace,
   visually distinct from the cyan RC feed and purple Quick Task. */
:root { --planner: #fbbf24; }

#planner-toggle {
  position: fixed; bottom: 20px; right: 124px; z-index: 201;
  width: 44px; height: 44px; border-radius: 50%;
  background: rgba(10, 14, 23, 0.85);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--planner);
  color: var(--planner); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 18px rgba(0,0,0,0.6);
  transition: transform 0.15s, box-shadow 0.15s;
}
#planner-toggle:hover {
  transform: scale(1.08);
  box-shadow: 0 0 0 2px var(--planner), 0 4px 18px rgba(0,0,0,0.6);
}
#planner-toggle.panel-open { opacity: 0; pointer-events: none; }
#planner-toggle .badge {
  position: absolute; top: -4px; right: -4px;
  min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: 9px;
  background: var(--planner); color: #0a0e17;
  font-size: 10px; font-weight: 800;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 0 0 2px var(--bg);
  font-variant-numeric: tabular-nums;
}
#planner-toggle .badge.zero { display: none; }

/* Panel: 380px wide, anchored bottom-right and shifted further left of
   the Quick Task panel (which sits at right:380px when open). Both can
   live open side-by-side on a 1200+ display without overlapping. */
#planner-panel {
  position: fixed; bottom: 20px; right: 760px;
  width: 380px; max-height: 70vh; z-index: 202;
  background: rgba(10, 14, 23, 0.85);
  backdrop-filter: blur(24px) saturate(1.2);
  -webkit-backdrop-filter: blur(24px) saturate(1.2);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.65);
  display: flex; flex-direction: column; overflow: hidden;
  transition: opacity 0.2s, transform 0.2s;
}
#planner-panel.hidden { opacity: 0; pointer-events: none; transform: translateY(8px); }

/* Header bar (drag handle) */
#planner-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px;
  background: rgba(255,255,255,0.03);
  border-bottom: 1px solid var(--border);
  cursor: grab; user-select: none; flex-shrink: 0;
}
#planner-bar:active { cursor: grabbing; }
#planner-title {
  display: flex; align-items: center; gap: 7px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
  text-transform: uppercase; color: var(--planner);
}
#planner-title .count {
  font-size: 10px; font-weight: 700; color: var(--text2);
  background: rgba(251, 191, 36, 0.10);
  border: 1px solid rgba(251, 191, 36, 0.25);
  padding: 1px 6px; border-radius: 8px; letter-spacing: 0;
  text-transform: none;
}
#planner-actions { display: flex; align-items: center; gap: 2px; }
#planner-actions button {
  background: none; border: none; color: var(--text2); cursor: pointer;
  padding: 3px 5px; line-height: 1; border-radius: 4px;
  display: inline-flex; align-items: center;
  font-size: 14px; font-weight: 600;
  transition: color 0.1s, background 0.1s;
}
#planner-actions button:hover { color: var(--planner); background: rgba(251, 191, 36, 0.10); }

/* Quick-add row */
#planner-add-row {
  padding: 10px 12px;
  display: flex; gap: 6px; align-items: center;
  border-bottom: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  flex-shrink: 0;
}
#planner-add-input {
  flex: 1; min-width: 0;
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 10px;
  color: var(--text); font-size: 12px;
  font-family: inherit; outline: none;
}
#planner-add-input::placeholder { color: var(--text2); opacity: 0.55; }
#planner-add-input:focus { border-color: var(--planner); }
#planner-add-btn {
  flex-shrink: 0;
  background: var(--planner); color: #0a0e17;
  border: none; border-radius: 7px;
  width: 30px; height: 30px;
  font-size: 18px; font-weight: 800; line-height: 1;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: filter 0.15s, transform 0.05s;
}
#planner-add-btn:hover  { filter: brightness(1.1); }
#planner-add-btn:active { transform: translateY(1px); }

/* Filter / project selectors */
#planner-controls {
  display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
#planner-controls select {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
  color: var(--text); font-size: 11px;
  outline: none; cursor: pointer;
  max-width: 160px;
}
#planner-controls select:focus { border-color: var(--planner); }
.planner-chip {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 3px 9px;
  color: var(--text2); font-size: 10px; font-weight: 600;
  cursor: pointer; user-select: none;
  transition: all 0.1s;
}
.planner-chip:hover { color: var(--text); border-color: var(--planner); }
.planner-chip.active {
  background: var(--planner); color: #0a0e17;
  border-color: var(--planner);
}

/* Task list (the scrolling area) */
#planner-list {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: 6px 0;
}
#planner-list::-webkit-scrollbar { width: 6px; }
#planner-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
#planner-list::-webkit-scrollbar-thumb:hover { background: var(--text2); }

.planner-empty {
  padding: 30px 16px; text-align: center;
  font-size: 11px; color: var(--text2);
  line-height: 1.6;
}
.planner-empty .big { font-size: 22px; opacity: 0.5; margin-bottom: 6px; }

.planner-task {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 8px 12px;
  border-bottom: 1px solid rgba(30, 45, 69, 0.4);
  transition: background 0.1s;
  position: relative;
}
.planner-task:hover { background: rgba(255,255,255,0.025); }
.planner-task.is-done { opacity: 0.5; }
.planner-task.is-doing { background: rgba(251, 191, 36, 0.04); }
.planner-task.is-overdue:not(.is-done) .due-pill { color: var(--red); border-color: var(--red); }

.planner-check {
  flex-shrink: 0; width: 18px; height: 18px;
  border-radius: 50%;
  border: 1.5px solid var(--text2);
  background: transparent;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  margin-top: 1px;
  transition: all 0.12s;
  position: relative;
}
.planner-check:hover { border-color: var(--planner); }
.planner-check.doing {
  border-color: var(--planner);
  background: linear-gradient(90deg, var(--planner) 50%, transparent 50%);
}
.planner-check.done {
  border-color: var(--green); background: var(--green);
}
.planner-check.done svg { stroke: #0a0e17; }
.planner-check svg { width: 11px; height: 11px; opacity: 0; transition: opacity 0.1s; }
.planner-check.done svg { opacity: 1; }

.planner-body {
  flex: 1; min-width: 0;
  display: flex; flex-direction: column; gap: 3px;
  cursor: pointer;
}
.planner-title {
  font-size: 12.5px; line-height: 1.4; color: var(--text);
  word-break: break-word;
}
.is-done .planner-title { text-decoration: line-through; color: var(--text2); }
.planner-meta {
  display: flex; flex-wrap: wrap; gap: 5px; align-items: center;
  font-size: 9.5px; color: var(--text2);
}
.planner-meta .pill {
  padding: 1px 6px; border-radius: 8px;
  border: 1px solid var(--border);
  display: inline-flex; align-items: center; gap: 3px;
  line-height: 1.5;
  background: rgba(0,0,0,0.25);
}
.planner-meta .proj-pill { color: var(--text); }
.planner-meta .proj-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--planner);
  display: inline-block;
}
.planner-meta .due-pill { color: var(--text); }
.planner-meta .agent-pill { color: var(--purple); border-color: rgba(167, 139, 250, 0.3); }
.planner-meta .prio-pill {
  padding: 1px 5px;
  font-weight: 700;
  letter-spacing: 0.4px;
}
.planner-meta .prio-pill.p1 { color: var(--accent); border-color: rgba(56, 189, 248, 0.3); }
.planner-meta .prio-pill.p2 { color: var(--yellow); border-color: rgba(245, 158, 11, 0.3); }
.planner-meta .prio-pill.p3 { color: var(--red);    border-color: rgba(239, 68, 68, 0.3); }

/* Expanded edit area */
.planner-task.expanded .planner-edit { display: flex; }
.planner-edit {
  display: none;
  margin-left: 28px; margin-top: 6px; margin-bottom: 4px;
  flex-direction: column; gap: 7px;
  padding: 8px; border-radius: 7px;
  background: rgba(0,0,0,0.30);
  border: 1px solid var(--border);
}
.planner-edit textarea {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 6px 8px;
  color: var(--text); font-size: 11px; line-height: 1.45;
  font-family: inherit; outline: none; resize: vertical;
  min-height: 50px; max-height: 200px;
  width: 100%;
}
.planner-edit textarea:focus { border-color: var(--planner); }
.planner-edit-row {
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
}
.planner-edit-row label {
  font-size: 9px; color: var(--text2); text-transform: uppercase;
  letter-spacing: 0.4px; font-weight: 600;
}
.planner-edit-row select,
.planner-edit-row input[type="date"],
.planner-edit-row input[type="text"] {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 4px 7px;
  color: var(--text); font-size: 11px;
  font-family: inherit; outline: none;
  color-scheme: dark;
}
.planner-edit-row select:focus,
.planner-edit-row input:focus { border-color: var(--planner); }
.planner-edit-row .grow { flex: 1; min-width: 80px; }
.planner-edit-actions {
  display: flex; gap: 6px; justify-content: flex-end; align-items: center;
  padding-top: 4px;
}
.planner-edit-actions .danger {
  background: none; color: var(--red);
  border: 1px solid rgba(239, 68, 68, 0.30);
  border-radius: 5px; padding: 4px 8px;
  font-size: 10px; font-weight: 600; cursor: pointer;
  transition: background 0.1s;
}
.planner-edit-actions .danger:hover { background: rgba(239, 68, 68, 0.10); }
.planner-edit-actions .save {
  background: var(--planner); color: #0a0e17;
  border: none; border-radius: 5px; padding: 4px 12px;
  font-size: 10px; font-weight: 700; cursor: pointer;
}
.planner-edit-actions .save:hover { filter: brightness(1.1); }

/* Footer */
#planner-footer {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 12px;
  border-top: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  font-size: 10px; color: var(--text2);
  flex-shrink: 0;
}
#planner-footer button {
  background: none; border: none; color: var(--text2);
  font-size: 10px; cursor: pointer;
  padding: 2px 6px; border-radius: 4px;
  transition: color 0.1s, background 0.1s;
}
#planner-footer button:hover { color: var(--planner); background: rgba(251, 191, 36, 0.10); }
#planner-status { color: var(--text2); }
#planner-status.ok    { color: var(--green); }
#planner-status.error { color: var(--red); }

/* ── Staff drawer (slides down between header and quick-add) ──────── */
#planner-staff-drawer {
  border-bottom: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  flex-shrink: 0;
  overflow: hidden;
  max-height: 0;
  transition: max-height 0.2s ease;
}
#planner-staff-drawer.open { max-height: 380px; overflow-y: auto; }
#planner-staff-drawer::-webkit-scrollbar { width: 6px; }
#planner-staff-drawer::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

.staff-section-title {
  font-size: 9.5px; color: var(--text2); text-transform: uppercase;
  letter-spacing: 0.5px; font-weight: 700;
  padding: 8px 12px 4px;
}
.staff-list { padding: 0 6px 6px; }
.staff-row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 8px; border-radius: 6px;
  font-size: 11.5px;
}
.staff-row:hover { background: rgba(255,255,255,0.025); }
.staff-row .name { font-weight: 600; color: var(--text); }
.staff-row .handle { color: var(--text2); font-size: 10.5px; }
.staff-row .grow { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.staff-row .open-badge {
  background: rgba(251, 191, 36, 0.14);
  color: var(--planner);
  border: 1px solid rgba(251, 191, 36, 0.30);
  border-radius: 8px; padding: 1px 6px;
  font-size: 9.5px; font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.staff-row .open-badge.zero {
  background: transparent; color: var(--text2);
  border-color: var(--border);
}
.staff-row .icon-btn {
  background: none; border: 1px solid var(--border);
  color: var(--text2); cursor: pointer;
  width: 24px; height: 22px; border-radius: 5px;
  display: inline-flex; align-items: center; justify-content: center;
  padding: 0; transition: color 0.1s, border-color 0.1s, background 0.1s;
}
.staff-row .icon-btn:hover { color: var(--planner); border-color: var(--planner); }
.staff-row .icon-btn.danger:hover { color: var(--red); border-color: var(--red); }
.staff-row .icon-btn svg { width: 11px; height: 11px; }

.staff-add-row {
  display: flex; gap: 6px; align-items: center;
  padding: 6px 12px 10px;
  position: relative;
}
.staff-add-row input {
  flex: 1; min-width: 0;
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 9px;
  color: var(--text); font-size: 11px;
  outline: none;
}
.staff-add-row input:focus { border-color: var(--planner); }
.staff-suggest {
  position: absolute; left: 12px; right: 12px; top: calc(100% - 6px);
  background: rgba(10, 14, 23, 0.95);
  backdrop-filter: blur(20px);
  border: 1px solid var(--border); border-radius: 7px;
  max-height: 200px; overflow-y: auto;
  z-index: 5;
  display: none;
  box-shadow: 0 6px 18px rgba(0,0,0,0.5);
}
.staff-suggest.open { display: block; }
.staff-suggest .row {
  display: flex; gap: 6px; align-items: center;
  padding: 6px 10px; cursor: pointer;
  font-size: 11px;
  border-bottom: 1px solid rgba(30, 45, 69, 0.4);
}
.staff-suggest .row:last-child { border-bottom: none; }
.staff-suggest .row:hover,
.staff-suggest .row.kbd { background: rgba(251, 191, 36, 0.10); }
.staff-suggest .row .handle { color: var(--text2); font-size: 10px; }
.staff-suggest .empty { padding: 10px; text-align: center; color: var(--text2); font-size: 10.5px; }

#planner-digest-strip {
  padding: 8px 12px;
  border-top: 1px solid var(--border);
  background: rgba(0,0,0,0.18);
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  font-size: 10.5px; color: var(--text2);
}
#planner-digest-strip label {
  font-size: 9px; color: var(--text2); text-transform: uppercase;
  letter-spacing: 0.4px; font-weight: 700;
}
#planner-digest-strip input[type="time"] {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 3px 6px;
  color: var(--text); font-size: 11px; font-family: inherit;
  outline: none; color-scheme: dark;
}
#planner-digest-strip input[type="time"]:focus { border-color: var(--planner); }
#planner-digest-strip .toggle {
  display: inline-flex; align-items: center; gap: 5px;
  cursor: pointer; user-select: none; font-size: 10.5px;
  color: var(--text);
}
#planner-digest-strip .toggle input { margin: 0; cursor: pointer; }
#planner-digest-strip .last-sent { flex: 1; min-width: 0; text-align: right; }

/* ── Assignee pill on collapsed task rows ─────────────────────────── */
.planner-meta .who-pill {
  border-color: rgba(34, 197, 94, 0.3);
  color: var(--green);
}
.planner-meta .who-pill.is-agent {
  border-color: rgba(167, 139, 250, 0.3);
  color: var(--purple);
}
.planner-meta .who-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor;
  display: inline-block;
}

/* assignee select uses optgroups; nothing extra needed */

/* responsive */
@media (max-width: 1240px) {
  #planner-panel { right: 12px; left: 12px; width: auto; bottom: 12px; max-height: 80vh; }
}

/* ── Calendar widget ──────────────────────────────────────────────────
   4th FAB sits LEFT of #planner-toggle (right:124px). 124+44+8 = 176.
   Emerald accent so the four tools (RC=cyan, Quick Task=purple,
   Planner=amber, Calendar=emerald) read as four distinct workspaces. */
:root { --calendar: #10b981; }

#calendar-toggle {
  position: fixed; bottom: 20px; right: 176px; z-index: 201;
  width: 44px; height: 44px; border-radius: 50%;
  background: rgba(10, 14, 23, 0.85);
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--calendar);
  color: var(--calendar); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 18px rgba(0,0,0,0.6);
  transition: transform 0.15s, box-shadow 0.15s;
}
#calendar-toggle:hover {
  transform: scale(1.08);
  box-shadow: 0 0 0 2px var(--calendar), 0 4px 18px rgba(0,0,0,0.6);
}
#calendar-toggle.panel-open { opacity: 0; pointer-events: none; }
#calendar-toggle .badge {
  position: absolute; top: -4px; right: -4px;
  min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: 9px;
  background: var(--calendar); color: #0a0e17;
  font-size: 10px; font-weight: 800;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 0 0 2px var(--bg);
  font-variant-numeric: tabular-nums;
}
#calendar-toggle .badge.zero { display: none; }

/* Panel: ~460px to fit a usable 7-col grid; positioned to default at the
   left of the planner column. User can drag elsewhere; position persists. */
#calendar-panel {
  position: fixed; bottom: 20px; right: 380px;
  width: 460px; max-height: 78vh; z-index: 202;
  background: rgba(10, 14, 23, 0.85);
  backdrop-filter: blur(24px) saturate(1.2);
  -webkit-backdrop-filter: blur(24px) saturate(1.2);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 12px 36px rgba(0,0,0,0.65);
  display: flex; flex-direction: column; overflow: hidden;
  transition: opacity 0.2s, transform 0.2s;
}
#calendar-panel.hidden { opacity: 0; pointer-events: none; transform: translateY(8px); }

#calendar-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 12px;
  background: rgba(255,255,255,0.03);
  border-bottom: 1px solid var(--border);
  cursor: grab; user-select: none; flex-shrink: 0;
}
#calendar-bar:active { cursor: grabbing; }
#calendar-title {
  display: flex; align-items: center; gap: 7px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
  text-transform: uppercase; color: var(--calendar);
}
#calendar-actions { display: flex; align-items: center; gap: 2px; }
#calendar-actions button {
  background: none; border: none; color: var(--text2); cursor: pointer;
  padding: 3px 5px; line-height: 1; border-radius: 4px;
  display: inline-flex; align-items: center;
  font-size: 14px; font-weight: 600;
  transition: color 0.1s, background 0.1s;
}
#calendar-actions button:hover { color: var(--calendar); background: rgba(16, 185, 129, 0.10); }

#calendar-nav {
  display: flex; align-items: center; gap: 4px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  flex-shrink: 0;
}
#calendar-nav button {
  background: none; border: 1px solid var(--border);
  color: var(--text2); cursor: pointer;
  width: 26px; height: 24px; border-radius: 5px;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 700; padding: 0;
  transition: color 0.1s, border-color 0.1s, background 0.1s;
}
#calendar-nav button:hover { color: var(--calendar); border-color: var(--calendar); }
#calendar-month-label {
  font-size: 13px; font-weight: 700; color: var(--text);
  margin: 0 6px;
  flex: 1; text-align: center;
  letter-spacing: 0.3px;
}
#calendar-today-btn {
  font-size: 10px !important; font-weight: 700 !important;
  padding: 0 8px !important; width: auto !important;
  text-transform: uppercase; letter-spacing: 0.4px;
}

/* The 7×N grid */
#calendar-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 1px;
  background: var(--border);
  flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
.cal-dow {
  background: rgba(0,0,0,0.30);
  padding: 4px 0; text-align: center;
  font-size: 9px; font-weight: 700;
  color: var(--text2);
  text-transform: uppercase; letter-spacing: 0.5px;
}
.cal-cell {
  background: rgba(10, 14, 23, 0.92);
  min-height: 64px; max-height: 88px;
  padding: 3px 4px;
  font-size: 10px;
  cursor: pointer;
  position: relative;
  overflow: hidden;
  transition: background 0.1s;
}
.cal-cell:hover { background: rgba(16, 185, 129, 0.06); }
.cal-cell.other-month { opacity: 0.35; }
.cal-cell.weekend .cal-day-num { color: var(--text2); }
.cal-cell.today {
  background: rgba(16, 185, 129, 0.08);
  box-shadow: inset 0 0 0 1px var(--calendar);
}
.cal-cell.selected {
  background: rgba(16, 185, 129, 0.14);
  box-shadow: inset 0 0 0 1px var(--calendar);
}
.cal-day-num {
  font-size: 10.5px; font-weight: 600; color: var(--text);
  line-height: 1.1; margin-bottom: 2px;
  font-variant-numeric: tabular-nums;
}
.cal-cell.today .cal-day-num {
  background: var(--calendar); color: #0a0e17;
  border-radius: 50%;
  width: 16px; height: 16px;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 9.5px;
}
.cal-pill {
  display: block;
  font-size: 9px; line-height: 1.3;
  padding: 1px 4px; margin-top: 1px;
  border-radius: 3px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  background: rgba(16, 185, 129, 0.15);
  color: var(--calendar);
  border-left: 2px solid var(--calendar);
}
.cal-pill.is-task {
  background: rgba(251, 191, 36, 0.12);
  color: var(--planner);
  border-left-color: var(--planner);
}
.cal-pill.is-done { opacity: 0.5; text-decoration: line-through; }
.cal-pill.is-overdue {
  background: rgba(239, 68, 68, 0.14);
  color: var(--red);
  border-left-color: var(--red);
}
.cal-more {
  display: block;
  font-size: 8.5px; color: var(--text2);
  margin-top: 1px;
  font-weight: 700;
}

/* Day detail strip */
#calendar-day-detail {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: 8px 12px;
  background: rgba(0,0,0,0.20);
}
#calendar-day-detail::-webkit-scrollbar { width: 6px; }
#calendar-day-detail::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

.cal-detail-title {
  font-size: 10px; font-weight: 700; color: var(--calendar);
  text-transform: uppercase; letter-spacing: 0.5px;
  margin-bottom: 8px;
}
.cal-detail-empty {
  padding: 18px 8px; text-align: center;
  font-size: 11px; color: var(--text2);
}
.cal-evt {
  display: flex; gap: 8px; align-items: flex-start;
  padding: 6px 4px; margin-bottom: 4px;
  border-radius: 6px;
  background: rgba(255,255,255,0.025);
  border-left: 3px solid var(--calendar);
  cursor: pointer;
  transition: background 0.1s;
}
.cal-evt:hover { background: rgba(16, 185, 129, 0.07); }
.cal-evt.is-task { border-left-color: var(--planner); background: rgba(251, 191, 36, 0.05); }
.cal-evt.is-task:hover { background: rgba(251, 191, 36, 0.10); }
.cal-evt .when {
  flex-shrink: 0;
  font-size: 10px; font-weight: 700;
  color: var(--text);
  font-variant-numeric: tabular-nums;
  width: 60px;
}
.cal-evt .when.notime { color: var(--text2); font-weight: 600; }
.cal-evt .body { flex: 1; min-width: 0; }
.cal-evt .title { font-size: 11.5px; color: var(--text); line-height: 1.35; word-break: break-word; }
.cal-evt .meta {
  font-size: 9.5px; color: var(--text2);
  margin-top: 2px;
}

/* Inline event editor (expanded under a clicked event) */
.cal-evt-edit {
  margin: 4px 0 8px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: rgba(0,0,0,0.30);
  display: flex; flex-direction: column; gap: 6px;
}
.cal-evt-edit input,
.cal-evt-edit textarea {
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 5px 8px;
  color: var(--text); font-size: 11px; font-family: inherit;
  outline: none; color-scheme: dark;
  width: 100%; box-sizing: border-box;
}
.cal-evt-edit textarea { resize: vertical; min-height: 40px; max-height: 160px; }
.cal-evt-edit input:focus,
.cal-evt-edit textarea:focus { border-color: var(--calendar); }
.cal-evt-edit-row {
  display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
}
.cal-evt-edit-row label {
  font-size: 9px; color: var(--text2);
  text-transform: uppercase; letter-spacing: 0.4px; font-weight: 700;
}
.cal-evt-edit-row .grow { flex: 1; min-width: 60px; }
.cal-evt-edit-actions {
  display: flex; gap: 6px; justify-content: flex-end;
}
.cal-evt-edit-actions .save {
  background: var(--calendar); color: #0a0e17;
  border: none; border-radius: 5px; padding: 4px 12px;
  font-size: 10px; font-weight: 700; cursor: pointer;
}
.cal-evt-edit-actions .save:hover { filter: brightness(1.1); }
.cal-evt-edit-actions .danger {
  background: none; color: var(--red);
  border: 1px solid rgba(239, 68, 68, 0.30);
  border-radius: 5px; padding: 4px 8px;
  font-size: 10px; font-weight: 600; cursor: pointer;
}
.cal-evt-edit-actions .danger:hover { background: rgba(239, 68, 68, 0.10); }
.cal-evt-edit-actions .cancel {
  background: none; color: var(--text2);
  border: 1px solid var(--border);
  border-radius: 5px; padding: 4px 8px;
  font-size: 10px; font-weight: 600; cursor: pointer;
}
.cal-evt-edit-actions .cancel:hover { color: var(--text); border-color: var(--text2); }

/* "+ Add event" button below day detail */
.cal-add-row {
  margin-top: 8px;
  display: flex; gap: 6px; align-items: center;
}
.cal-add-row input {
  flex: 1; min-width: 0;
  background: rgba(0,0,0,0.35);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 9px;
  color: var(--text); font-size: 11px;
  outline: none;
}
.cal-add-row input:focus { border-color: var(--calendar); }
.cal-add-row input.time {
  flex: 0 0 70px;
  font-variant-numeric: tabular-nums;
  text-align: center;
  color-scheme: dark;
}
.cal-add-row button {
  flex-shrink: 0;
  background: var(--calendar); color: #0a0e17;
  border: none; border-radius: 6px;
  width: 28px; height: 28px;
  font-size: 16px; font-weight: 800; line-height: 1;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
}
.cal-add-row button:hover { filter: brightness(1.1); }

#calendar-status {
  font-size: 10px; color: var(--text2);
  padding: 6px 12px;
  border-top: 1px solid var(--border);
  background: rgba(255,255,255,0.02);
  flex-shrink: 0;
}
#calendar-status.ok    { color: var(--green); }
#calendar-status.error { color: var(--red); }

/* responsive */
@media (max-width: 1240px) {
  #calendar-panel { right: 12px; left: 12px; width: auto; bottom: 12px; max-height: 85vh; }
}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-left">
    <h1>JARVIS v4</h1>
    <nav class="hdr-nav">
      <button class="hdr-nav-link" id="map-link">Map</button>
      <button class="hdr-nav-link" data-section="docs">Docs</button>
      <button class="hdr-nav-link" data-section="apps">Apps</button>
      <button class="hdr-nav-link" data-section="modules">Modules</button>
      <button class="hdr-nav-link" data-section="agents">Agents</button>
    </nav>
  </div>
  <div class="hdr-right">
    <button id="deploy-btn">+ Deploy Agent</button>
    <button id="migrate-btn" title="Migrate an agent from JARVIS v2">↗ Migrate v2</button>
    <div class="view-toggle-group">
      <button class="view-toggle-btn active" id="view-tile-btn" title="Tile view">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
          <rect x="1" y="1" width="6" height="6" rx="1.5"/><rect x="9" y="1" width="6" height="6" rx="1.5"/>
          <rect x="1" y="9" width="6" height="6" rx="1.5"/><rect x="9" y="9" width="6" height="6" rx="1.5"/>
        </svg>
      </button>
      <button class="view-toggle-btn" id="view-compact-btn" title="Compact view">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
          <rect x="1" y="2" width="14" height="2" rx="1"/><rect x="1" y="6" width="14" height="2" rx="1"/>
          <rect x="1" y="10" width="14" height="2" rx="1"/><rect x="1" y="14" width="14" height="2" rx="1"/>
        </svg>
      </button>
    </div>
    <label class="hdr-online-toggle">
      <input type="checkbox" id="online-only-toggle">
      <span>Online only</span>
    </label>
    <button class="hdr-btn" id="az-btn">A–Z grid</button>
    <button class="hdr-btn" id="recent-btn" title="Order by most recent dispatch activity">Recent grid</button>
    <span class="hdr-clock" id="clock"></span>
  </div>
</div>

<!-- tag sub-nav -->
<div id="tag-bar">
  <span id="tag-bar-label">Filter:</span>
  <div id="tag-bar-search">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input id="agent-search" type="text" placeholder="Search agents…" autocomplete="off" spellcheck="false">
    <button id="agent-search-clear" title="Clear">×</button>
  </div>
</div>

<!-- file browser panel -->
<div id="browser-panel">
  <div id="browser-tree"></div>
  <div id="browser-content">
    <div id="browser-toolbar">
      <span id="browser-file-path">Select a file</span>
      <button id="browser-close">×</button>
    </div>
    <div id="browser-body"><div style="color:var(--text2);font-size:13px;padding:40px;text-align:center;opacity:0.4;">Select a file from the tree</div></div>
  </div>
</div>

<div id="canvas">
  <div id="map-hint">Drag to arrange · cards update every 5s</div>
  <div id="canvas-floor"></div>
</div>

<!-- xterm terminal modal -->
<div id="term-overlay">
  <div id="term-modal">
    <div id="term-bar">
      <span id="term-title">Terminal</span>
      <span id="term-status">—</span>
      <div style="display:flex;align-items:center;gap:6px;margin-left:auto;">
        <button id="term-copy" title="Copy selection (or all output)" style="background:none;border:1px solid rgba(255,255,255,0.12);border-radius:5px;color:var(--text2);font-size:11px;padding:2px 8px;cursor:pointer;display:flex;align-items:center;gap:4px;">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          Copy
        </button>
        <button id="term-close" title="Close (Esc)">×</button>
      </div>
    </div>
    <div id="term-container"></div>
  </div>
</div>

<!-- toast -->
<div id="toast"></div>

<!-- RC config popover -->
<div id="rc-popover">
  <div id="rc-pop-bar">
    <span id="rc-pop-title">Rocket.Chat</span>
    <button id="rc-pop-close">×</button>
  </div>
  <div id="rc-pop-body"></div>
  <div id="rc-pop-actions">
    <button class="rc-pop-btn rc-pop-btn-kill"    id="rc-pop-kill">Kill Monitor</button>
    <button class="rc-pop-btn rc-pop-btn-restart" id="rc-pop-restart">Restart Monitor</button>
  </div>
</div>

<!-- Mail config popover -->
<div id="mail-popover">
  <div id="mail-pop-bar">
    <span id="mail-pop-title">Mail</span>
    <button id="mail-pop-close">×</button>
  </div>
  <div id="mail-pop-body"></div>
  <div id="mail-pop-actions">
    <button class="mail-pop-btn mail-pop-btn-test" id="mail-pop-test">Test Connection</button>
  </div>
  <pre id="mail-pop-output"></pre>
</div>

<!-- Browser config popover -->
<div id="br-popover">
  <div id="br-pop-bar">
    <span id="br-pop-title">Browser</span>
    <button id="br-pop-close">×</button>
  </div>
  <div id="br-pop-body"></div>
  <div id="br-pop-go-row">
    <input type="text" id="br-pop-url" placeholder="https://..." spellcheck="false">
    <button id="br-pop-go">Go</button>
  </div>
  <div id="br-pop-actions">
    <button class="br-pop-btn br-pop-btn-launch" id="br-pop-launch">Launch</button>
    <button class="br-pop-btn br-pop-btn-stop"   id="br-pop-stop">Stop</button>
    <button class="br-pop-btn br-pop-btn-shot"   id="br-pop-shot">Snapshot</button>
    <button class="br-pop-btn br-pop-btn-test"   id="br-pop-test">Test</button>
  </div>
  <pre id="br-pop-output"></pre>
  <img id="br-pop-preview" alt="" />
</div>

<!-- Agent info panel -->
<div id="agent-info-overlay">
  <div id="agent-info-panel">
    <div id="agent-info-bar">
      <span id="agent-info-title">Agent</span>
      <div style="display:flex;align-items:center;gap:8px;margin-left:auto;">
        <button id="agent-info-edit" title="Edit file" style="display:none;">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          Edit
        </button>
        <button id="agent-info-close">×</button>
      </div>
    </div>
    <div id="agent-info-tabs">
      <button class="agent-info-tab active" data-tab="context">Context</button>
      <button class="agent-info-tab" data-tab="utilities">Utilities</button>
      <button class="agent-info-tab" data-tab="routines">Routines</button>
      <button class="agent-info-tab" data-tab="files">Files</button>
      <button class="agent-info-tab" data-tab="tags">Settings</button>
      <button class="agent-info-tab" data-tab="logs">Logs</button>
      <button class="agent-info-tab" data-tab="git">Git</button>
    </div>
    <div id="agent-info-body"><div id="agent-info-loading">Loading…</div></div>
  </div>
</div>

<!-- Deploy Agent modal -->
<div id="deploy-overlay">
  <div id="deploy-modal">
    <div id="deploy-bar">
      <span id="deploy-bar-title">Deploy New Agent</span>
      <button id="deploy-close">×</button>
    </div>
    <div id="deploy-form">
      <div class="deploy-section-hdr">Agent Identity</div>
      <div class="deploy-field">
        <label>Agent Name <span style="color:var(--red)">*</span></label>
        <input id="d-name" type="text" placeholder="e.g. example.com" autocomplete="off">
      </div>
      <label class="deploy-check-row" style="margin-bottom:14px;">
        <input type="checkbox" id="d-master">
        <span>Master Agent <span style="font-size:13px;">👑</span></span>
        <span style="margin-left:6px;font-size:10px;color:var(--text2);opacity:0.7;">— distinct card style</span>
      </label>
      <div class="deploy-field">
        <label>Poll Interval (seconds)</label>
        <input id="d-interval" type="number" value="10" min="5" max="300">
      </div>
      <div class="deploy-section-hdr">RocketChat</div>
      <label class="deploy-check-row">
        <input type="checkbox" id="d-no-channel"> Skip channel creation
      </label>
      <label class="deploy-check-row">
        <input type="checkbox" id="d-no-webhook"> Skip webhook registration
      </label>
      <div class="deploy-section-hdr">Mail Inbox <span style="opacity:0.5;font-weight:400;text-transform:none;font-size:10px;">(optional)</span></div>
      <div class="deploy-field">
        <label>Mail Host</label>
        <input id="d-mb-host" type="text" placeholder="mail.example.com">
      </div>
      <div class="deploy-field">
        <label>Email Address</label>
        <input id="d-mb-email" type="text" placeholder="agent@example.com">
      </div>
      <div class="deploy-field">
        <label>Password</label>
        <input id="d-mb-pass" type="password" placeholder="IMAP/SMTP password">
      </div>
    </div>
    <pre id="deploy-output"></pre>
    <div id="deploy-footer">
      <button id="deploy-run">Deploy</button>
      <span id="deploy-status"></span>
    </div>
  </div>
</div>

<!-- Migrate v2 -> v4 modal -->
<div id="migrate-overlay">
  <div id="migrate-modal">
    <div id="migrate-bar">
      <span id="migrate-bar-title">Migrate Agent from v2</span>
      <button id="migrate-close">×</button>
    </div>
    <div id="migrate-form">
      <div class="deploy-section-hdr">Source (v2)</div>
      <div class="deploy-field">
        <label>v2 Agent <span style="color:var(--red)">*</span></label>
        <select id="m-source"><option value="">Loading…</option></select>
        <div id="migrate-source-meta"></div>
      </div>

      <div class="deploy-section-hdr">Target (v4)</div>
      <div class="deploy-field">
        <label>v4 Agent Name <span style="color:var(--red)">*</span></label>
        <input id="m-target" type="text" placeholder="auto-suggested">
        <div id="migrate-target-meta"></div>
      </div>

      <div class="deploy-section-hdr">What to copy</div>
      <div class="migrate-check-list">
        <label class="deploy-check-row" style="margin:0;">
          <input type="checkbox" id="m-opt-context" checked> context.md (merge)
        </label>
        <label class="deploy-check-row" style="margin:0;">
          <input type="checkbox" id="m-opt-routines" checked> routines/
        </label>
        <label class="deploy-check-row" style="margin:0;">
          <input type="checkbox" id="m-opt-utilities" checked> utilities/ (filtered)
        </label>
        <label class="deploy-check-row" style="margin:0;">
          <input type="checkbox" id="m-opt-docs" checked> docs/
        </label>
        <label class="deploy-check-row" style="margin:0;">
          <input type="checkbox" id="m-opt-jobs" checked> jobs/done/
        </label>
      </div>

      <div class="deploy-section-hdr">After migration</div>
      <div class="migrate-check-list">
        <label class="deploy-check-row" style="margin:0;" title="Move legacy v2 agent to archive after successful migration (requires JARVIS_V2_ROOT).">
          <input type="checkbox" id="m-opt-archive" checked> Archive v2 source dir (mv to jarvisv2/archive/)
        </label>
      </div>

      <div class="deploy-section-hdr">Migration Plan</div>
      <div id="migrate-summary"><div class="ms-empty">Pick a source agent to see the plan…</div></div>
    </div>
    <pre id="migrate-output"></pre>
    <div id="migrate-footer">
      <button id="migrate-preview-btn">Preview</button>
      <button id="migrate-run-btn">Migrate</button>
      <span id="migrate-status"></span>
    </div>
  </div>
</div>

<!-- Add App / App Config modal -->
<div id="appmgr-overlay">
  <div id="appmgr-modal">
    <div id="appmgr-bar">
      <span id="appmgr-title">Manage Apps</span>
      <button id="appmgr-close">×</button>
    </div>
    <div id="appmgr-inner">
      <!-- left: app list -->
      <div id="appmgr-sidebar">
        <div id="appmgr-sidebar-label">Available Apps</div>
        <div id="appmgr-tabs"></div>
      </div>
      <!-- right: config detail -->
      <div id="appmgr-detail">
        <div id="appmgr-detail-header">
          <div id="appmgr-detail-title">Select an app</div>
          <div id="appmgr-detail-desc"></div>
        </div>
        <div id="appmgr-body">
          <div id="appmgr-fields"></div>
        </div>
        <div id="appmgr-footer">
          <button id="appmgr-save">Install</button>
          <button id="appmgr-delete">Remove</button>
          <span id="appmgr-msg"></span>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
(function() {
'use strict';

// ── constants ──
var CARD_W = 400, CARD_H = 420, GAP = 24, COLS = 4, TOP = 18, LEFT = 20;
var STORAGE_POS = 'j4_positions';
var STORAGE_OO  = 'j4_online_only';
var canvas = document.getElementById('canvas');
var agents = {};        // name → {el, data}
// ── clock ──
function updateClock() {
  var d = new Date();
  document.getElementById('clock').textContent =
    d.toLocaleDateString() + '  ' + d.toLocaleTimeString();
}
updateClock();
setInterval(updateClock, 1000);

// ── toast ──
var toastTimer = null;
function toast(msg, dur) {
  var el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function() { el.classList.remove('show'); }, dur || 2500);
}

// ── positions ──
function loadPos() {
  try { return JSON.parse(localStorage.getItem(STORAGE_POS) || '{}'); } catch(e) { return {}; }
}
function savePos(p) {
  try { localStorage.setItem(STORAGE_POS, JSON.stringify(p)); } catch(e) {}
}
function calcCols() {
  // Use actual rendered card width if available, fall back to CARD_W constant
  var cards = Object.values(agents);
  var w = cards.length ? cards[0].el.offsetWidth : CARD_W;
  var available = window.innerWidth - LEFT * 2;
  return Math.max(1, Math.floor((available + GAP) / (w + GAP)));
}
function defaultPos(idx, isMaster, masterRow, hasMasters) {
  var cols = calcCols();
  if (isMaster) {
    return {
      x: LEFT + (cols - 1) * (CARD_W + GAP),
      y: TOP  + masterRow * (CARD_H + GAP)
    };
  }
  // If there are masters, keep regulars out of the rightmost column
  var regularCols = hasMasters ? Math.max(1, cols - 1) : cols;
  return {
    x: LEFT + (idx % regularCols) * (CARD_W + GAP),
    y: TOP  + Math.floor(idx / regularCols) * (CARD_H + GAP)
  };
}

// ── online-only toggle ──
var onlineOnly = false;
try { onlineOnly = localStorage.getItem(STORAGE_OO) === '1'; } catch(e) {}
var ooToggle = document.getElementById('online-only-toggle');
ooToggle.checked = onlineOnly;
ooToggle.addEventListener('change', function() {
  onlineOnly = this.checked;
  try { localStorage.setItem(STORAGE_OO, onlineOnly ? '1' : '0'); } catch(e) {}
  applyVisibility();
});

function applyVisibility() {
  Object.values(agents).forEach(function(a) {
    if (onlineOnly && !a.data.online) {
      a.el.style.display = 'none';
    } else {
      a.el.style.display = '';
    }
  });
}

// ── A–Z grid ──
// Tile view → fixed 6 columns (canvas scrolls horizontally if the viewport
// can't fit them — that's intentional, the user asked for 6 regardless of
// width). Compact view keeps the dynamic fit-to-width behaviour.
var AZ_TILE_COLS = 6;

// Pack a list of visible agents into a top-left A-Z grid (regulars fill
// columns left-to-right, masters float to the rightmost column). Shared by
// the A-Z button and the search-as-you-type auto-pack.
function packGrid(visible) {
  var pos  = loadPos();
  var cols = (_viewMode === 'tile') ? AZ_TILE_COLS : calcCols();

  var sampleEl = visible.length ? visible[0].el : null;
  var cardW = sampleEl ? sampleEl.offsetWidth  : CARD_W;
  var cardH = sampleEl ? sampleEl.offsetHeight : CARD_H;

  var masters     = visible.filter(function(a) { return  a.data.is_master; });
  var regulars    = visible.filter(function(a) { return !a.data.is_master; });
  var regularCols = masters.length ? Math.max(1, cols - 1) : cols;

  regulars.forEach(function(a, i) {
    var x = LEFT + (i % regularCols) * (cardW + GAP);
    var y = TOP  + Math.floor(i / regularCols) * (cardH + GAP);
    a.el.style.left = x + 'px';
    a.el.style.top  = y + 'px';
    pos[a.data.name] = {x: x, y: y};
  });
  masters.forEach(function(a, i) {
    var x = LEFT + (cols - 1) * (cardW + GAP);
    var y = TOP  + i * (cardH + GAP);
    a.el.style.left = x + 'px';
    a.el.style.top  = y + 'px';
    pos[a.data.name] = {x: x, y: y};
  });
  savePos(pos);
  expandFloor();
}

document.getElementById('az-btn').addEventListener('click', function() {
  var visible = Object.values(agents)
    .filter(function(a) {
      return (!onlineOnly || a.data.online) && a.el.dataset.tagHidden !== '1';
    })
    .sort(function(a, b) { return a.data.name.localeCompare(b.data.name); });
  packGrid(visible);
});

// Recent-activity sort: most-recent dispatch.log mtime first.
// Reuses the same packGrid() pipeline as A–Z so master-column reservation
// and tile-vs-compact behaviour stay identical — only the order changes.
document.getElementById('recent-btn').addEventListener('click', function() {
  var visible = Object.values(agents)
    .filter(function(a) {
      return (!onlineOnly || a.data.online) && a.el.dataset.tagHidden !== '1';
    })
    .sort(function(a, b) {
      return (b.data.last_activity || 0) - (a.data.last_activity || 0);
    });
  packGrid(visible);
});

// ── view toggle (tile / list / compact) ──
var STORAGE_VIEW = 'j4_view';
var _viewMode = 'tile';
try { _viewMode = localStorage.getItem(STORAGE_VIEW) || 'tile'; } catch(e) {}

function applyViewMode(mode) {
  if (mode === 'list') mode = 'tile'; // list view removed — fall back to tile
  _viewMode = mode;
  try { localStorage.setItem(STORAGE_VIEW, mode); } catch(e) {}
  canvas.classList.toggle('compact-view', mode === 'compact');
  document.getElementById('view-tile-btn').classList.toggle('active',    mode === 'tile');
  document.getElementById('view-compact-btn').classList.toggle('active', mode === 'compact');
  if (mode === 'compact') snapListView();
  else expandFloor();
}

function snapListView() {
  var pos    = loadPos();
  var sorted = Object.values(agents)
    .filter(function(a) { return a.el.dataset.tagHidden !== '1' && (!onlineOnly || a.data.online); })
    .sort(function(a, b) { return a.data.name.localeCompare(b.data.name); });
  // measure after CSS has applied (use actual height or fallback to 40px row)
  var rowH = sorted.length ? (sorted[0].el.offsetHeight || 40) : 40;
  sorted.forEach(function(a, i) {
    var x = LEFT;
    var y = TOP + i * (rowH + 6);
    a.el.style.left = x + 'px';
    a.el.style.top  = y + 'px';
    pos[a.data.name] = {x: x, y: y};
  });
  savePos(pos);
  expandFloor();
}

applyViewMode(_viewMode);
document.getElementById('view-tile-btn').addEventListener('click', function() { applyViewMode('tile'); });
document.getElementById('view-compact-btn').addEventListener('click', function() { applyViewMode('compact'); });

// ── SVG icons ──
var ICONS = {
  stop:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>',
  start:   '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4l14 8-14 8z"/></svg>',
  refresh: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>',
  term:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
  rc:      '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14l4 4V4a2 2 0 0 0-2-2z"/></svg>',
  settings:'<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  restart: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>',
};

// ── helpers ──
// One source of truth for the model pill — used by both makeCard()
// (initial render) and updateCard() (live refresh after model change).
function buildModelPill(modelSlug) {
  var el = document.createElement('span');
  var fam = (modelSlug.indexOf('composer') === 0) ? 'composer'
          : (modelSlug.indexOf('claude')   === 0) ? 'claude'
          : (modelSlug.indexOf('gpt')      === 0) ? 'gpt'
          : 'other';
  el.className = 'model-pill model-' + fam;
  el.title = 'Cursor model: ' + modelSlug;
  el.textContent = modelSlug
    .replace(/^claude-/, '')
    .replace(/-medium-thinking$/, ' THINK')
    .replace(/-medium$/, '')
    .replace(/-high$/, '')
    .replace(/-fast$/, ' FAST')
    .toUpperCase();
  return el;
}

// ── create card DOM ──
function makeCard(data, idx, masterRow, hasMasters) {
  var pos = loadPos();
  var p   = pos[data.name] || defaultPos(idx, data.is_master, masterRow || 0, hasMasters);

  var el = document.createElement('div');
  el.className = 'agent-card ' + (data.online ? 'online' : 'offline') + (data.is_master ? ' master' : '');
  el.dataset.name  = data.name;
  el.dataset.agent = data.name;
  el.dataset.master = data.is_master ? '1' : '';
  el.style.left = p.x + 'px';
  el.style.top  = p.y + 'px';

  // left zone
  var left = document.createElement('div');
  left.className = 'card-left';

  // top row
  var top = document.createElement('div');
  top.className = 'card-top';

  var dot = document.createElement('div');
  dot.className = 'status-dot ' + (data.online ? 'online' : 'offline');

  var nameEl = document.createElement('div');
  nameEl.className = 'card-name';
  nameEl.title = data.name;
  nameEl.innerHTML = escHtml(data.name) + (data.is_master ? '<span class="master-crown" title="Master agent">MASTER</span>' : '');

  // model pill — colour-keyed by family so you can scan tiles at a glance.
  // Lives as a top-row sibling (not a child of .card-name) so the name can
  // ellipsize without clipping the pill. flex-shrink:0 in CSS keeps it visible.
  var modelPill = data.model ? buildModelPill(data.model) : null;

  var badge = document.createElement('span');
  badge.className = 'ctx-badge';

  var infoBtn = document.createElement('button');
  infoBtn.className = 'card-info-btn';
  infoBtn.title = 'Agent settings';
  infoBtn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
  infoBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    openAgentInfo(data.name);
  });

  // dispatch count + ctx % badge — sit in top row before settings button
  var dispEl = document.createElement('span');
  dispEl.className = 'dispatch-count';
  if (data.dispatches > 0) dispEl.textContent = data.dispatches + '▲';

  var ctxBadge = document.createElement('span');
  ctxBadge.className = 'ctx-badge';

  top.appendChild(dot);
  top.appendChild(nameEl);
  top.appendChild(dispEl);
  top.appendChild(ctxBadge);
  top.appendChild(infoBtn);
  top.appendChild(badge);

  // Sub-row directly under the domain name — holds the model pill on its own
  // line. Always present (even when empty) so updateCard() can drop a pill in
  // later without juggling sibling order.
  var modelRow = document.createElement('div');
  modelRow.className = 'card-model-row';
  if (modelPill) modelRow.appendChild(modelPill);

  // plabel row removed — kept as empty placeholder for ref compat
  var plabelRow = document.createElement('div');
  plabelRow.className = 'plabel-row';
  plabelRow.style.display = 'none';

  // pane preview + copy button
  var paneWrap = document.createElement('div');
  paneWrap.className = 'pane-wrap';

  var preview = document.createElement('div');
  preview.className = 'pane-preview';
  preview.innerHTML = '<span class="pane-empty">' + (data.online ? 'loading…' : 'offline') + '</span>';

  var paneCopyBtn = document.createElement('button');
  paneCopyBtn.className = 'pane-copy-btn';
  paneCopyBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy';
  paneCopyBtn.title = 'Copy pane output';
  paneCopyBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    var lines = Array.from(preview.querySelectorAll('.pane-line')).map(function(el) { return el.textContent; });
    if (!lines.length) return;
    navigator.clipboard.writeText(lines.join('\n')).then(function() {
      paneCopyBtn.classList.add('copied');
      paneCopyBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copied!';
      setTimeout(function() {
        paneCopyBtn.classList.remove('copied');
        paneCopyBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy';
      }, 1400);
    }).catch(function(){});
  });

  paneWrap.appendChild(preview);
  paneWrap.appendChild(paneCopyBtn);

  // footer — action buttons only (status row removed)
  var footer = document.createElement('div');
  footer.className = 'card-footer';

  // action buttons row
  var actions = document.createElement('div');
  actions.className = 'card-actions';

  function mkBtn(cls, icon, label, handler) {
    var b = document.createElement('button');
    b.className = 'card-btn ' + cls;
    b.innerHTML = icon + '<span>' + label + '</span>';
    b.addEventListener('click', function(e) {
      e.stopPropagation();
      handler(data.name, data.session);
    });
    return b;
  }

  var stopBtn     = mkBtn('btn-stop',     ICONS.stop,     'Sleep',    doSleep);
  var startBtn    = mkBtn('btn-start',    ICONS.start,    'Start',    doStart);
  var refreshBtn  = mkBtn('btn-refresh',  ICONS.refresh,  'Refresh',  doRefresh);
  var logBtn      = mkBtn('btn-log',      ICONS.term,     'Term',     doLog);
  var settingsBtn = mkBtn('btn-settings', ICONS.settings, 'Settings', function(name) { openAppMgr(name); });
  var restartBtn  = mkBtn('btn-restart',  ICONS.restart,  'Restart',  function(name, session) {
    doStop(name, session);
    setTimeout(function() { doStart(name, session); }, 2000);
  });

  // compact-only buttons hidden in tile/list; tile/list-only hidden in compact
  stopBtn.dataset.hideCompact    = '1';
  startBtn.dataset.hideCompact   = '1';
  refreshBtn.dataset.hideCompact = '1';
  settingsBtn.dataset.compactOnly = '1';
  restartBtn.dataset.compactOnly  = '1';

  actions.appendChild(stopBtn);
  actions.appendChild(startBtn);
  actions.appendChild(refreshBtn);
  actions.appendChild(logBtn);
  actions.appendChild(settingsBtn);
  actions.appendChild(restartBtn);

  footer.appendChild(actions);

  left.appendChild(top);

  // tag chips row (below card-top, above pane label)
  var cardTagsRow = document.createElement('div');
  cardTagsRow.className = 'card-tags';
  cardTagsRow.dataset.tagsFor = data.name;
  (data.tags || []).forEach(function(t) {
    var chip = document.createElement('span');
    chip.className = 'card-tag-chip';
    chip.textContent = t;
    cardTagsRow.appendChild(chip);
  });
  left.appendChild(cardTagsRow);

  left.appendChild(plabelRow);
  left.appendChild(paneWrap);
  left.appendChild(modelRow);
  left.appendChild(footer);

  // right zone — apps panel (RC status only)
  var right = document.createElement('div');
  right.className = 'card-right';

  // RC app icon
  var rcIcon = document.createElement('div');
  rcIcon.className = 'app-icon app-icon-rc';

  var rcBubble = document.createElement('div');
  rcBubble.className = 'app-icon-bubble';
  rcBubble.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M20 2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14l4 4V4a2 2 0 0 0-2-2z"/></svg>';

  var rcDot = document.createElement('div');
  rcDot.className = 'app-badge ' + monitorStatusClass(data.monitor_status);
  rcDot.title = monitorStatusTitle(data);
  rcBubble.appendChild(rcDot);

  var rcLbl = document.createElement('div');
  rcLbl.className = 'app-icon-label';
  rcLbl.textContent = 'Rocket.Chat';

  var rcAge = document.createElement('div');
  rcAge.style.cssText = 'font-size:8px;color:var(--text2);';
  if (data.monitor_age >= 0) rcAge.textContent = data.monitor_age + 'm ago';

  rcIcon.appendChild(rcBubble);
  rcIcon.appendChild(rcLbl);
  rcIcon.appendChild(rcAge);

  rcIcon.style.cursor = 'pointer';
  rcBubble.addEventListener('click', function(e) {
    e.stopPropagation();
    e.preventDefault();
    if (rcPopover.classList.contains('open') && _rcPopAgent === data.name) {
      closeRcPop();
    } else {
      openRcPop(data.name, rcIcon);
    }
  });

  right.appendChild(rcIcon);

  // Mail icon — only shown if mailinbox.py is deployed for this agent
  if (data.has_mailinbox) {
    var mailIcon = document.createElement('div');
    mailIcon.className = 'app-icon app-icon-mail';

    var mailBubble = document.createElement('div');
    mailBubble.className = 'app-icon-bubble';
    mailBubble.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M20 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 2-8 5-8-5h16zm0 12H4V8l8 5 8-5v10z"/></svg>';

    var mailLbl = document.createElement('div');
    mailLbl.className = 'app-icon-label';
    mailLbl.textContent = 'Mail';

    mailIcon.appendChild(mailBubble);
    mailIcon.appendChild(mailLbl);
    mailIcon.style.cursor = 'pointer';

    mailBubble.addEventListener('click', function(e) {
      e.stopPropagation();
      e.preventDefault();
      if (mailPopover.classList.contains('open') && _mailPopAgent === data.name) {
        closeMailPop();
      } else {
        openMailPop(data.name, mailIcon);
      }
    });

    right.appendChild(mailIcon);
  }

  // Browser icon — only shown if browser.py is deployed for this agent
  if (data.has_browser) {
    var brIcon = document.createElement('div');
    brIcon.className = 'app-icon app-icon-browser';

    var brBubble = document.createElement('div');
    brBubble.className = 'app-icon-bubble';
    brBubble.style.background = 'linear-gradient(135deg, #a78bfa 0%, #5b21b6 100%)';
    // Globe glyph — sized to match the mail icon
    brBubble.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';

    var brLbl = document.createElement('div');
    brLbl.className = 'app-icon-label';
    brLbl.textContent = 'Browser';

    brIcon.appendChild(brBubble);
    brIcon.appendChild(brLbl);
    brIcon.style.cursor = 'pointer';

    brBubble.addEventListener('click', function(e) {
      e.stopPropagation();
      e.preventDefault();
      if (brPopover.classList.contains('open') && _brPopAgent === data.name) {
        closeBrPop();
      } else {
        openBrPop(data.name, brIcon);
      }
    });

    right.appendChild(brIcon);
  }

  // Add App (+) button — always shown at the bottom of the dock
  var addBtn = document.createElement('button');
  addBtn.className = 'app-add-btn';
  addBtn.title = 'Add / configure apps';
  addBtn.innerHTML = '+';
  addBtn.addEventListener('click', function(e) {
    e.stopPropagation();
    openAppMgr(data.name);
  });
  right.appendChild(addBtn);

  el.appendChild(left);
  el.appendChild(right);

  // store refs
  el._dot      = dot;
  el._preview  = preview;
  el._disp     = dispEl;
  el._rcDot    = rcDot;
  el._rcAge    = rcAge;
  el._badge    = badge;
  el._ctxBadge = ctxBadge;
  el._workTimer = null;

  makeDraggable(el);
  canvas.appendChild(el);
  return el;
}

// ── update existing card DOM ──
function updateCard(el, data) {
  el.classList.toggle('online',  data.online);
  el.classList.toggle('offline', !data.online);
  el.classList.toggle('master',  !!data.is_master);
  el.style.borderLeftColor = data.online ? '' : '';

  el._dot.className = 'status-dot ' + (data.online ? 'online' : 'offline');
  el._disp.textContent = data.dispatches > 0 ? data.dispatches + '▲' : '';

  // RC dot
  el._rcDot.className = 'app-badge ' + monitorStatusClass(data.monitor_status);
  el._rcDot.title = monitorStatusTitle(data);
  el._rcAge.textContent = data.monitor_age >= 0 ? data.monitor_age + 'm ago' : '';

  // refresh model pill — strip old, drop new into the dedicated row below
  // the domain name. Single-pill row, so we just clear + append.
  var modelRow = el.querySelector('.card-model-row');
  if (modelRow) {
    modelRow.innerHTML = '';
    if (data.model) modelRow.appendChild(buildModelPill(data.model));
  }

  if (!data.online) {
    el._preview.innerHTML = '<span class="pane-empty">offline</span>';
  }

  // refresh tag chips
  var tagsRow = el.querySelector('.card-tags');
  if (tagsRow) {
    tagsRow.innerHTML = '';
    (data.tags || []).forEach(function(t) {
      var chip = document.createElement('span');
      chip.className = 'card-tag-chip';
      chip.textContent = t;
      tagsRow.appendChild(chip);
    });
  }
}

// ── draggable ──
function makeDraggable(el) {
  var drag = null;
  el.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;
    if (e.target.closest('.card-btn, .app-icon')) return;
    e.preventDefault();
    var r = el.getBoundingClientRect();
    drag = { ox: e.clientX - r.left, oy: e.clientY - r.top };
    el.classList.add('dragging');
    el.style.zIndex = 999;
  });
  document.addEventListener('mousemove', function(e) {
    if (!drag) return;
    var cr = canvas.getBoundingClientRect();
    var x = Math.max(0, e.clientX - cr.left + canvas.scrollLeft - drag.ox);
    var y = Math.max(0, e.clientY - cr.top  + canvas.scrollTop  - drag.oy);
    el.style.left = x + 'px';
    el.style.top  = y + 'px';
    expandFloor();
  });
  document.addEventListener('mouseup', function() {
    if (!drag) return;
    drag = null;
    el.classList.remove('dragging');
    el.style.zIndex = '';
    var pos = loadPos();
    pos[el.dataset.name] = { x: parseInt(el.style.left), y: parseInt(el.style.top) };
    savePos(pos);
    expandFloor();
  });
}

function expandFloor() {
  var floor = document.getElementById('canvas-floor');
  if (!floor) return;
  var maxX = 0, maxY = 0;
  document.querySelectorAll('.agent-card').forEach(function(c) {
    maxX = Math.max(maxX, parseInt(c.style.left || 0) + c.offsetWidth);
    maxY = Math.max(maxY, parseInt(c.style.top  || 0) + c.offsetHeight);
  });
  floor.style.width  = (maxX + 120) + 'px';
  floor.style.height = (maxY + 120) + 'px';
}

// ── pane snapshots ──
var snapshotTimer = null;
function fetchSnapshots() {
  var online = Object.values(agents).filter(function(a) { return a.data.online; });
  if (!online.length) return;
  var params = online.map(function(a) { return 'session=' + encodeURIComponent(a.data.session); }).join('&');
  fetch('/api/pane/snapshots?' + params)
    .then(function(r) { return r.json(); })
    .then(function(d) {
      online.forEach(function(a) {
        var raw = d[a.data.session] || '';
        var lines = raw.split('\n').filter(function(l) { return l.trim(); });
        var tail  = lines.slice(-12);
        if (!tail.length) {
          a.el._preview.innerHTML = '<span class="pane-empty">waiting…</span>';
          return;
        }
        // detect change for glow
        var newHash = tail.join('|');
        if (a.el._lastHash !== undefined && newHash !== a.el._lastHash) {
          triggerGlow(a.el);
        }
        a.el._lastHash = newHash;

        updateCtxBadge(a.el, extractCtxPct(raw));

        a.el._preview.innerHTML = tail.map(function(l, i) {
          var fresh = i === tail.length - 1 ? ' fresh' : '';
          return '<div class="pane-line' + fresh + '">' + escHtml(l) + '</div>';
        }).join('');
      });
    })
    .catch(function() {});
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Heartbeat -> badge class/title (shared by tile create + tile update paths)
function monitorStatusClass(status) {
  if (status === 'alive')       return 'alive';
  if (status === 'stale')       return 'stale';
  if (status === 'hibernated')  return 'hibernated';
  if (status === 'waking')      return 'waking';
  if (status === 'disabled')    return 'disabled';
  if (status && status !== 'none') return 'dead';
  return '';
}

function monitorStatusTitle(data) {
  var s = data.monitor_status;
  if (s === 'hibernated') return 'Hibernated — dashboard is watching for new RC messages and will wake this agent automatically.';
  if (s === 'waking')     return 'Waking up — redeploy in flight…';
  if (s === 'disabled')   return 'Disabled (master off) — dashboard will not auto-wake this agent. Use Wake now or change mode to Auto to bring it back.';
  return 'RC monitor: ' + s + (data.monitor_age >= 0 ? ' (' + data.monitor_age + 'm ago)' : '');
}

function extractCtxPct(snap) {
  /* Cursor outputs something like: "Composer 1.5 · 51.4% · 10 files edited"
     Scan the last 15 lines backwards for any NN% pattern. */
  var lines = snap.split('\n').slice(-15);
  for (var i = lines.length - 1; i >= 0; i--) {
    var m = lines[i].match(/(\d{1,3}(?:\.\d+)?)\s*%/);
    if (m) {
      var v = parseFloat(m[1]);
      if (v >= 0 && v <= 100) return v;
    }
  }
  return null;
}

function updateCtxBadge(el, pct) {
  var badge = el._ctxBadge;
  if (!badge) return;
  if (pct === null) { badge.className = 'ctx-badge'; badge.textContent = ''; return; }
  var tier = pct < 50 ? 'ctx-low' : pct < 80 ? 'ctx-mid' : 'ctx-high';
  badge.className = 'ctx-badge visible ' + tier;
  badge.textContent = pct.toFixed(pct % 1 === 0 ? 0 : 1) + '%';
  badge.title = 'Context window: ' + pct.toFixed(1) + '%';
}

function triggerGlow(el) {
  el.classList.add('working');
  clearTimeout(el._workTimer);
  el._workTimer = setTimeout(function() { el.classList.remove('working'); }, 30000);
}

// ── fetch agents ──
function fetchAgents() {
  fetch('/api/agents')
    .then(function(r) { return r.json(); })
    .then(function(list) {
      window._agents = list;
      var seen = {};
      var hasMasters = list.some(function(d) { return d.is_master; });
      var masterRow = 0;
      var regularIdx = 0;
      list.forEach(function(data) {
        seen[data.name] = true;
        if (agents[data.name]) {
          agents[data.name].data = data;
          updateCard(agents[data.name].el, data);
        } else {
          var el = makeCard(data, regularIdx, masterRow, hasMasters);
          agents[data.name] = { el: el, data: data };
        }
        if (data.is_master) masterRow++; else regularIdx++;
      });
      // remove stale cards
      Object.keys(agents).forEach(function(n) {
        if (!seen[n]) {
          agents[n].el.remove();
          delete agents[n];
        }
      });
      applyVisibility();
      fetchSnapshots();
      buildTagBar();
      if (_activeTag) filterCardsByTag(_activeTag);
      expandFloor();
    })
    .catch(function() {});
}

fetchAgents();
setInterval(fetchAgents, 5000);
setInterval(fetchSnapshots, 2500);

// ── actions ──
function apiPost(url, name, msg) {
  toast('⏳ ' + msg + '…');
  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok !== false) toast('✓ ' + msg + ' done');
    else toast('✗ ' + (d.error || 'failed'), 3500);
    setTimeout(fetchAgents, 1200);
  })
  .catch(function(e) { toast('✗ ' + e, 3500); });
}

function doStop(name)    { apiPost('/api/stop',    name, 'Stopping ' + name); }
function doStart(name)   { apiPost('/api/start',   name, 'Starting ' + name); }
function doRefresh(name) { apiPost('/api/refresh', name, 'Refreshing ' + name); }
// Manual hibernate — same behaviour as the 24h auto-hibernate watcher. The
// hibernation endpoint snapshots the RC room cursor + flags the agent in
// the hibernation doc so wake-on-message fires when a new RC message hits.
// Note the URL path carries the name; the apiPost body is harmless extra
// noise the endpoint ignores.
function doSleep(name)   { apiPost('/api/agent/hibernation/' + encodeURIComponent(name) + '/hibernate', name, 'Sleeping ' + name); }

function doLog(name, session) {
  openTerm(name, session);
}

// ── xterm terminal modal ──
var termOverlay = document.getElementById('term-overlay');
var termTitle   = document.getElementById('term-title');
var termStatus  = document.getElementById('term-status');
var termCont    = document.getElementById('term-container');

var _term = null;
var _fitAddon = null;
var _termWs = null;
var _termResizeObs = null;

document.getElementById('term-close').addEventListener('click', closeTerm);
termOverlay.addEventListener('click', function(e) {
  if (e.target === termOverlay) closeTerm();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (termOverlay.classList.contains('open')) closeTerm();
    else if (appmgrOverlay.classList.contains('open')) closeAppMgr();
  }
});

function openTerm(name, session) {
  closeTerm();

  termTitle.textContent = name + ' · pane 1';
  termStatus.textContent = 'connecting…';
  termStatus.style.color = 'var(--text2)';
  termOverlay.classList.add('open');

  // Create xterm instance
  _term = new Terminal({
    theme: {
      background:   '#0d1117',
      foreground:   '#c9d1d9',
      cursor:       '#58a6ff',
      black:        '#0d1117',
      red:          '#ff7b72',
      green:        '#3fb950',
      yellow:       '#d29922',
      blue:         '#58a6ff',
      magenta:      '#bc8cff',
      cyan:         '#39c5cf',
      white:        '#b1bac4',
      brightBlack:  '#6e7681',
      brightRed:    '#ffa198',
      brightGreen:  '#56d364',
      brightYellow: '#e3b341',
      brightBlue:   '#79c0ff',
      brightMagenta:'#d2a8ff',
      brightCyan:   '#56d4dd',
      brightWhite:  '#f0f6fc',
    },
    fontFamily: "'SF Mono', 'Fira Code', 'Cascadia Code', monospace",
    fontSize: 13,
    lineHeight: 1.4,
    cursorBlink: true,
    scrollback: 5000,
    allowProposedApi: true,
  });

  _fitAddon = new FitAddon.FitAddon();
  _term.loadAddon(_fitAddon);
  _term.loadAddon(new WebLinksAddon.WebLinksAddon());
  _term.open(termCont);
  _fitAddon.fit();

  // WebSocket connection
  var proto = location.protocol === 'https:' ? 'wss' : 'ws';
  var wsUrl = proto + '://' + location.host + '/ws/tmux/' + encodeURIComponent(session);
  _termWs = new WebSocket(wsUrl);
  _termWs.binaryType = 'arraybuffer';

  _termWs.onopen = function() {
    termStatus.textContent = 'live';
    termStatus.style.color = 'var(--green)';
    _sendResize();
  };

  _termWs.onmessage = function(e) {
    var data = e.data instanceof ArrayBuffer
      ? new Uint8Array(e.data)
      : e.data;
    _term.write(data);
  };

  _termWs.onclose = function() {
    termStatus.textContent = 'disconnected';
    termStatus.style.color = 'var(--red)';
    if (_term) _term.write('\r\n\x1b[31m[connection closed]\x1b[0m\r\n');
  };

  _termWs.onerror = function() {
    termStatus.textContent = 'error';
    termStatus.style.color = 'var(--red)';
  };

  // Forward keystrokes to server
  _term.onData(function(data) {
    if (_termWs && _termWs.readyState === WebSocket.OPEN) {
      _termWs.send(data);
    }
  });

  // Copy / Paste keyboard shortcuts
  // Ctrl+C with active selection → copy (don't send ETX to PTY)
  // Ctrl+V → paste from clipboard into PTY
  _term.attachCustomKeyEventHandler(function(ev) {
    if (ev.type !== 'keydown') return true;
    if (ev.ctrlKey && ev.key === 'c' && _term.hasSelection()) {
      navigator.clipboard.writeText(_term.getSelection()).catch(function(){});
      _flashCopied();
      return false; // swallow — don't send Ctrl+C to PTY
    }
    if (ev.ctrlKey && ev.key === 'v') {
      navigator.clipboard.readText().then(function(text) {
        if (_termWs && _termWs.readyState === WebSocket.OPEN) {
          _termWs.send(text);
        }
      }).catch(function(){});
      return false; // swallow — we handle it above
    }
    return true;
  });

  // Copy button in toolbar
  document.getElementById('term-copy').onclick = function() {
    var text = _term.hasSelection() ? _term.getSelection() : _termGetAllText();
    if (!text) return;
    navigator.clipboard.writeText(text).then(_flashCopied).catch(function(){});
  };

  // Send resize on window resize
  _termResizeObs = new ResizeObserver(function() {
    if (_fitAddon) { _fitAddon.fit(); _sendResize(); }
  });
  _termResizeObs.observe(termCont);
}

function _sendResize() {
  if (!_term || !_termWs || _termWs.readyState !== WebSocket.OPEN) return;
  var cols = _term.cols, rows = _term.rows;
  // Resize message prefix: \x01r<cols>,<rows>
  _termWs.send('\x01r' + cols + ',' + rows);
}

function _flashCopied() {
  var btn = document.getElementById('term-copy');
  if (!btn) return;
  btn.classList.add('copied');
  setTimeout(function() { btn.classList.remove('copied'); }, 1200);
}

function _termGetAllText() {
  if (!_term) return '';
  var lines = [];
  var buf = _term.buffer.active;
  for (var i = 0; i < buf.length; i++) {
    var line = buf.getLine(i);
    if (line) lines.push(line.translateToString(true));
  }
  // Trim trailing blank lines
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  return lines.join('\n');
}

function closeTerm() {
  if (_termResizeObs) { _termResizeObs.disconnect(); _termResizeObs = null; }
  if (_termWs)  { _termWs.close();  _termWs  = null; }
  if (_term)    { _term.dispose();   _term    = null; }
  _fitAddon = null;
  termCont.innerHTML = '';
  termOverlay.classList.remove('open');
}

// ── RC config popover ──
var rcPopover    = document.getElementById('rc-popover');
var rcPopBody    = document.getElementById('rc-pop-body');
var rcPopTitle   = document.getElementById('rc-pop-title');
var _rcPopAgent  = null;

document.getElementById('rc-pop-close').addEventListener('click', closeRcPop);
document.addEventListener('click', function(e) {
  if (rcPopover.classList.contains('open') &&
      !rcPopover.contains(e.target) &&
      !e.target.closest('.app-icon')) {
    closeRcPop();
  }
});

document.getElementById('rc-pop-kill').addEventListener('click', function() {
  if (!_rcPopAgent) return;
  this.textContent = 'Killing…';
  var btn = this;
  fetch('/api/rc/kill', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: _rcPopAgent})
  }).then(function(r) { return r.json(); }).then(function(d) {
    btn.textContent = d.killed && d.killed.length ? 'Killed ✓' : 'Not running';
    setTimeout(function() { btn.textContent = 'Kill Monitor'; }, 2000);
    showToast('RC monitor killed');
  });
});

document.getElementById('rc-pop-restart').addEventListener('click', function() {
  if (!_rcPopAgent) return;
  this.textContent = 'Restarting…';
  var btn = this;
  fetch('/api/rc/restart', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: _rcPopAgent})
  }).then(function(r) { return r.json(); }).then(function(d) {
    btn.textContent = 'Restarted ✓';
    setTimeout(function() { btn.textContent = 'Restart Monitor'; }, 2000);
    showToast('RC monitor restarted');
  });
});

function openRcPop(name, anchorEl) {
  _rcPopAgent = name;
  rcPopTitle.textContent = 'Rocket.Chat · ' + name;
  rcPopBody.innerHTML = '<div style="padding:8px 0;font-size:10px;color:var(--text2);">Loading…</div>';
  rcPopover.classList.add('open');

  // Position popover to the right of the anchor icon
  var r = anchorEl.getBoundingClientRect();
  var top = Math.min(r.top, window.innerHeight - 320);
  var left = r.right + 12;
  // If it would go off screen right, flip to left
  if (left + 300 > window.innerWidth) left = r.left - 312;
  rcPopover.style.top  = top + 'px';
  rcPopover.style.left = left + 'px';

  fetch('/api/rc/config/' + encodeURIComponent(name))
    .then(function(r) { return r.json(); })
    .then(function(cfg) {
      var labels = {
        DEFAULT_CHANNEL:      'Channel',
        DEFAULT_USER:         'Bot User',
        DEFAULT_INTERVAL:     'Poll Interval',
        DEFAULT_WEBHOOK_URL:  'Webhook URL',
        DEFAULT_TMUX_SESSION: 'Tmux Session',
      };
      var html = '';
      Object.keys(labels).forEach(function(k) {
        var val = cfg[k] || '—';
        html += '<div class="rc-cfg-row">' +
          '<span class="rc-cfg-key">' + labels[k] + '</span>' +
          '<span class="rc-cfg-val">' + escHtml(val) + '</span>' +
          '</div>';
      });
      rcPopBody.innerHTML = html;
    });
}

function closeRcPop() {
  rcPopover.classList.remove('open');
  _rcPopAgent = null;
}

// ── Mail popover ──
var mailPopover   = document.getElementById('mail-popover');
var mailPopBody   = document.getElementById('mail-pop-body');
var mailPopTitle  = document.getElementById('mail-pop-title');
var mailPopOutput = document.getElementById('mail-pop-output');
var _mailPopAgent = null;

document.getElementById('mail-pop-close').addEventListener('click', closeMailPop);
document.addEventListener('click', function(e) {
  if (mailPopover.classList.contains('open') &&
      !mailPopover.contains(e.target) &&
      !e.target.closest('.app-icon-mail')) {
    closeMailPop();
  }
});

document.getElementById('mail-pop-test').addEventListener('click', function() {
  if (!_mailPopAgent) return;
  var btn = this;
  btn.textContent = 'Testing…';
  mailPopOutput.style.display = 'block';
  mailPopOutput.textContent = 'Connecting…';
  fetch('/api/mailinbox/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: _mailPopAgent})
  }).then(function(r) { return r.json(); }).then(function(d) {
    btn.textContent = 'Test Connection';
    mailPopOutput.textContent = d.output || (d.ok ? 'OK' : 'Failed');
    mailPopOutput.style.color = d.ok ? 'var(--green)' : 'var(--red)';
  }).catch(function(e) {
    btn.textContent = 'Test Connection';
    mailPopOutput.textContent = 'Error: ' + e;
    mailPopOutput.style.color = 'var(--red)';
  });
});

function openMailPop(name, anchorEl) {
  _mailPopAgent = name;
  mailPopTitle.textContent = 'Mail · ' + name;
  mailPopBody.innerHTML = '<div style="padding:8px 0;font-size:10px;color:var(--text2);">Loading…</div>';
  mailPopOutput.style.display = 'none';
  mailPopOutput.textContent = '';
  mailPopover.classList.add('open');

  var r = anchorEl.getBoundingClientRect();
  var top  = Math.min(r.top, window.innerHeight - 280);
  var left = r.right + 12;
  if (left + 300 > window.innerWidth) left = r.left - 312;
  mailPopover.style.top  = top + 'px';
  mailPopover.style.left = left + 'px';

  fetch('/api/mailinbox/config/' + encodeURIComponent(name))
    .then(function(r) { return r.json(); })
    .then(function(cfg) {
      if (cfg.error) {
        mailPopBody.innerHTML = '<div style="padding:8px;font-size:11px;color:var(--red);">' + escHtml(cfg.error) + '</div>';
        return;
      }
      var labels = {
        DEFAULT_HOST:      'Host',
        DEFAULT_EMAIL:     'Email',
        DEFAULT_INBOX:     'Default Inbox',
        DEFAULT_IMAP_PORT: 'IMAP Port',
        DEFAULT_SMTP_PORT: 'SMTP Port',
      };
      var html = '';
      Object.keys(labels).forEach(function(k) {
        var val = cfg[k] || '—';
        html += '<div class="rc-cfg-row">' +
          '<span class="rc-cfg-key">' + labels[k] + '</span>' +
          '<span class="rc-cfg-val">' + escHtml(val) + '</span>' +
          '</div>';
      });
      mailPopBody.innerHTML = html;
    });
}

function closeMailPop() {
  mailPopover.classList.remove('open');
  _mailPopAgent = null;
}

// ── Browser popover ──
var brPopover   = document.getElementById('br-popover');
var brPopBody   = document.getElementById('br-pop-body');
var brPopTitle  = document.getElementById('br-pop-title');
var brPopOutput = document.getElementById('br-pop-output');
var brPopPreview= document.getElementById('br-pop-preview');
var brPopUrl    = document.getElementById('br-pop-url');
var _brPopAgent = null;

document.getElementById('br-pop-close').addEventListener('click', closeBrPop);
document.addEventListener('click', function(e) {
  if (brPopover.classList.contains('open') &&
      !brPopover.contains(e.target) &&
      !e.target.closest('.app-icon-browser')) {
    closeBrPop();
  }
});

function brOut(msg, ok) {
  brPopOutput.style.display = 'block';
  brPopOutput.textContent = msg;
  brPopOutput.style.color = ok === false ? 'var(--red)' :
                            ok === true  ? 'var(--green)' : 'var(--text2)';
}

function brAction(args, label) {
  if (!_brPopAgent) return Promise.resolve();
  brOut('Working…');
  return fetch('/api/browser/' + args.path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({name: _brPopAgent}, args.body || {}))
  }).then(function(r) { return r.json(); }).then(function(d) {
    brOut((d.output || '').toString() || (d.ok ? 'OK' : 'Failed'), !!d.ok);
    if (d.ok && args.refreshAfter !== false) brRefresh();
    return d;
  }).catch(function(e) {
    brOut('Error: ' + e, false);
  });
}

document.getElementById('br-pop-launch').addEventListener('click', function() {
  brAction({path: 'launch'}, 'launch');
});
document.getElementById('br-pop-stop').addEventListener('click', function() {
  brAction({path: 'stop'}, 'stop');
});
document.getElementById('br-pop-test').addEventListener('click', function() {
  brAction({path: 'test'}, 'test');
});
document.getElementById('br-pop-shot').addEventListener('click', function() {
  if (!_brPopAgent) return;
  brOut('Capturing screenshot…');
  brPopPreview.classList.remove('visible');
  // cache-bust so we don't show stale shots
  brPopPreview.onload  = function() { brPopPreview.classList.add('visible'); brOut('Screenshot updated', true); };
  brPopPreview.onerror = function() { brOut('Screenshot failed', false); };
  brPopPreview.src = '/api/browser/screenshot/' + encodeURIComponent(_brPopAgent) + '?t=' + Date.now();
});
document.getElementById('br-pop-go').addEventListener('click', function() {
  var u = brPopUrl.value.trim();
  if (!u) { brPopUrl.focus(); return; }
  brAction({path: 'goto', body: {url: u}}, 'goto');
});
brPopUrl.addEventListener('keydown', function(e) {
  if (e.key === 'Enter') document.getElementById('br-pop-go').click();
});

function brRefresh() {
  if (!_brPopAgent) return;
  fetch('/api/browser/config/' + encodeURIComponent(_brPopAgent))
    .then(function(r) { return r.json(); })
    .then(function(cfg) {
      if (cfg.error) {
        brPopBody.innerHTML = '<div style="padding:8px;font-size:11px;color:var(--red);">' + escHtml(cfg.error) + '</div>';
        return;
      }
      var dot = '<span class="br-status-dot ' + (cfg.running ? 'running' : 'stopped') + '"></span>';
      var rows = [
        ['Status',  dot + (cfg.running ? 'Running' : 'Stopped')],
        ['PID',     cfg.pid || '—'],
        ['Port',    cfg.port || '—'],
        ['Headless', cfg.headless === null || cfg.headless === undefined ? '—' : (cfg.headless ? 'yes' : 'no')],
        ['Last URL', cfg.last_url || '—'],
        ['Started', cfg.started_at || '—'],
        ['Profile', cfg.profile_dir || '—'],
      ];
      var html = '';
      rows.forEach(function(r) {
        html += '<div class="rc-cfg-row">' +
          '<span class="rc-cfg-key">' + r[0] + '</span>' +
          '<span class="rc-cfg-val">' + (r[0] === 'Status' ? r[1] : escHtml(String(r[1]))) + '</span>' +
          '</div>';
      });
      brPopBody.innerHTML = html;
    });
}

function openBrPop(name, anchorEl) {
  _brPopAgent = name;
  brPopTitle.textContent = 'Browser · ' + name;
  brPopBody.innerHTML = '<div style="padding:8px 0;font-size:10px;color:var(--text2);">Loading…</div>';
  brPopOutput.style.display = 'none';
  brPopOutput.textContent = '';
  brPopPreview.classList.remove('visible');
  brPopPreview.src = '';
  brPopUrl.value = '';
  brPopover.classList.add('open');

  var r = anchorEl.getBoundingClientRect();
  var top  = Math.min(r.top, window.innerHeight - 460);
  var left = r.right + 12;
  if (left + 332 > window.innerWidth) left = r.left - 332;
  brPopover.style.top  = top + 'px';
  brPopover.style.left = left + 'px';

  brRefresh();
}

function closeBrPop() {
  brPopover.classList.remove('open');
  _brPopAgent = null;
}

// ── App Manager modal ──
var appmgrOverlay     = document.getElementById('appmgr-overlay');
var appmgrTitle       = document.getElementById('appmgr-title');
var appmgrTabs        = document.getElementById('appmgr-tabs');
var appmgrFields      = document.getElementById('appmgr-fields');
var appmgrSave        = document.getElementById('appmgr-save');
var appmgrDelete      = document.getElementById('appmgr-delete');
var appmgrMsg         = document.getElementById('appmgr-msg');
var appmgrDetailTitle = document.getElementById('appmgr-detail-title');
var appmgrDetailDesc  = document.getElementById('appmgr-detail-desc');
var _appmgrAgent   = null;
var _appmgrAppId   = null;
var _appmgrRegistry  = null;
var _appmgrInstalled = null;

document.getElementById('appmgr-close').addEventListener('click', closeAppMgr);
appmgrOverlay.addEventListener('click', function(e) {
  if (e.target === appmgrOverlay) closeAppMgr();
});

appmgrSave.addEventListener('click', function() {
  if (!_appmgrAgent || !_appmgrAppId) return;
  var fields = {};
  appmgrFields.querySelectorAll('input[data-key]').forEach(function(inp) {
    fields[inp.dataset.key] = inp.value;
  });
  appmgrSave.disabled = true;
  appmgrSave.textContent = 'Installing…';
  showAppMgrMsg('', false);
  fetch('/api/apps/install', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({agent: _appmgrAgent, app_id: _appmgrAppId, fields: fields})
  }).then(function(r) { return r.json(); }).then(function(d) {
    appmgrSave.disabled = false;
    appmgrSave.textContent = 'Save & Install';
    if (d.ok) {
      showAppMgrMsg('✓ ' + (d.message || 'Installed'), false);
      _appmgrInstalled[_appmgrAppId] = true;
      renderAppmgrTabs();
      fetchAgents();
    } else {
      showAppMgrMsg('✗ ' + (d.message || 'Failed'), true);
    }
  }).catch(function(e) {
    appmgrSave.disabled = false;
    appmgrSave.textContent = 'Save & Install';
    showAppMgrMsg('✗ ' + e, true);
  });
});

appmgrDelete.addEventListener('click', function() {
  if (!_appmgrAgent || !_appmgrAppId) return;
  if (!confirm('Remove ' + (_appmgrRegistry[_appmgrAppId] || {label: _appmgrAppId}).label + ' from ' + _appmgrAgent + '?')) return;
  appmgrDelete.disabled = true;
  fetch('/api/apps/remove', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({agent: _appmgrAgent, app_id: _appmgrAppId})
  }).then(function(r) { return r.json(); }).then(function(d) {
    appmgrDelete.disabled = false;
    if (d.ok) {
      _appmgrInstalled[_appmgrAppId] = false;
      appmgrDelete.classList.remove('visible');
      appmgrSave.textContent = 'Install';
      appmgrFields.querySelectorAll('input').forEach(function(i) { i.value = ''; });
      showAppMgrMsg('✓ Removed', false);
      renderAppmgrTabs();
      fetchAgents();
    } else {
      showAppMgrMsg('✗ ' + (d.error || d.message || 'Failed'), true);
    }
  }).catch(function(e) {
    appmgrDelete.disabled = false;
    showAppMgrMsg('✗ ' + e, true);
  });
});

function showAppMgrMsg(msg, isErr) {
  appmgrMsg.textContent = msg;
  appmgrMsg.className = isErr ? 'err' : '';
  appmgrMsg.style.display = msg ? 'inline' : 'none';
}

function openAppMgr(agentName) {
  _appmgrAgent = agentName;
  _appmgrAppId = null;
  appmgrTitle.textContent = 'Apps · ' + agentName;
  appmgrTabs.innerHTML = '<span style="font-size:11px;color:var(--text2);padding:8px;">Loading…</span>';
  appmgrFields.innerHTML = '';
  appmgrDetailTitle.textContent = 'Select an app';
  appmgrDetailDesc.textContent  = '';
  showAppMgrMsg('', false);
  appmgrOverlay.classList.add('open');

  // Load registry + installed state in parallel
  Promise.all([
    fetch('/api/apps/registry').then(function(r) { return r.json(); }),
    fetch('/api/apps/installed/' + encodeURIComponent(agentName)).then(function(r) { return r.json(); })
  ]).then(function(results) {
    _appmgrRegistry  = results[0];
    _appmgrInstalled = results[1];
    renderAppmgrTabs();
    // Auto-select first non-builtin uninstalled, or first tab
    var ids = Object.keys(_appmgrRegistry);
    var first = ids.find(function(id) { return !_appmgrRegistry[id].builtin && !_appmgrInstalled[id]; })
             || ids[0];
    if (first) selectAppmgrTab(first);
  }).catch(function() {
    appmgrTabs.innerHTML = '<span style="color:var(--red);font-size:11px;">Failed to load registry</span>';
  });
}

function renderAppmgrTabs() {
  appmgrTabs.innerHTML = '';
  Object.keys(_appmgrRegistry).forEach(function(id) {
    var spec      = _appmgrRegistry[id];
    var installed = _appmgrInstalled[id];

    var row = document.createElement('div');
    row.className = 'appmgr-app-row' + (_appmgrAppId === id ? ' active' : '');

    var dot = document.createElement('div');
    dot.className = 'appmgr-app-dot';
    dot.style.background = spec.color;

    var name = document.createElement('div');
    name.className = 'appmgr-app-name';
    name.textContent = spec.label;

    row.appendChild(dot);
    row.appendChild(name);

    if (installed) {
      var badge = document.createElement('span');
      badge.className = 'appmgr-app-badge';
      badge.textContent = spec.builtin ? 'Built-in' : 'On';
      row.appendChild(badge);
    }

    row.addEventListener('click', function() { selectAppmgrTab(id); });
    appmgrTabs.appendChild(row);
  });
}

function selectAppmgrTab(id) {
  _appmgrAppId = id;
  showAppMgrMsg('', false);
  renderAppmgrTabs();

  var spec      = _appmgrRegistry[id];
  var installed = _appmgrInstalled[id];

  // update detail header
  document.getElementById('appmgr-detail-title').textContent = spec.label;
  document.getElementById('appmgr-detail-desc').textContent  =
    installed
      ? (spec.builtin ? 'Built-in app — always present. Edit configuration below.' : 'Installed — edit configuration below.')
      : 'Not installed. Fill in the fields below and click Install.';

  appmgrSave.textContent = installed ? 'Save Config' : 'Install';
  // Show Remove button only for installed, non-builtin apps
  if (installed && !spec.builtin) {
    appmgrDelete.classList.add('visible');
  } else {
    appmgrDelete.classList.remove('visible');
  }
  appmgrDelete.disabled = false;

  appmgrFields.innerHTML = '<div style="font-size:10px;color:var(--text2);margin-bottom:10px;">Loading config…</div>';

  fetch('/api/apps/config/' + encodeURIComponent(_appmgrAgent) + '/' + encodeURIComponent(id))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      renderAppmgrFields(spec.fields, data.values || {}, installed);
    }).catch(function() {
      renderAppmgrFields(spec.fields, {}, installed);
    });
}

function renderAppmgrFields(fields, values, installed) {
  appmgrFields.innerHTML = '';
  if (!fields || fields.length === 0) {
    appmgrFields.innerHTML = '<div style="font-size:11px;color:var(--text2);padding:8px 0;">No configurable fields.</div>';
    return;
  }
  fields.forEach(function(field) {
    var wrap  = document.createElement('div');
    wrap.className = 'appmgr-field';

    var lbl = document.createElement('label');
    lbl.textContent = field.label;

    var inp = document.createElement('input');
    inp.type       = field.secret ? 'password' : 'text';
    inp.dataset.key = field.key;
    inp.value      = values[field.key] || '';
    inp.placeholder = installed ? '(unchanged)' : '';
    if (field.secret && installed && inp.value) {
      inp.placeholder = '(leave blank to keep current)';
    }

    wrap.appendChild(lbl);
    wrap.appendChild(inp);
    appmgrFields.appendChild(wrap);
  });
}

function closeAppMgr() {
  appmgrOverlay.classList.remove('open');
  _appmgrAgent = null;
  _appmgrAppId = null;
}

// ── Agent info panel ──
var agentInfoOverlay = document.getElementById('agent-info-overlay');
var agentInfoTitle   = document.getElementById('agent-info-title');
var agentInfoBody    = document.getElementById('agent-info-body');
var agentInfoEditBtn = document.getElementById('agent-info-edit');
var _infoAgent       = null;
var _infoTab         = 'context';
var _infoRawContent  = null; // raw text of last loaded file (for edit)

document.getElementById('agent-info-close').addEventListener('click', closeAgentInfo);
agentInfoOverlay.addEventListener('click', function(e) {
  if (e.target === agentInfoOverlay) closeAgentInfo();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && agentInfoOverlay.classList.contains('open')) closeAgentInfo();
});

agentInfoEditBtn.addEventListener('click', function() {
  if (agentInfoEditBtn.classList.contains('editing')) {
    _saveContextEdit();
  } else {
    _enterContextEdit();
  }
});

document.querySelectorAll('.agent-info-tab').forEach(function(btn) {
  btn.addEventListener('click', function() {
    _infoTab = this.dataset.tab;
    document.querySelectorAll('.agent-info-tab').forEach(function(b) { b.classList.remove('active'); });
    this.classList.add('active');
    if (_infoAgent) loadInfoTab(_infoAgent, _infoTab);
  });
});

function openAgentInfo(name) {
  _infoAgent = name;
  _infoTab = 'context';
  agentInfoTitle.textContent = name;
  document.querySelectorAll('.agent-info-tab').forEach(function(b) {
    b.classList.toggle('active', b.dataset.tab === 'context');
  });
  agentInfoBody.innerHTML = '<div id="agent-info-loading">Loading…</div>';
  agentInfoOverlay.classList.add('open');
  loadInfoTab(name, 'context');
}

function loadInfoTab(name, tab) {
  // stop any periodic timer set by previous tab (e.g. logs auto-refresh)
  if (window._infoTabTimer) { clearInterval(window._infoTabTimer); window._infoTabTimer = null; }
  // reset body styles set by special tabs (files) before each render
  agentInfoBody.style.cssText = '';
  agentInfoBody.innerHTML = '<div id="agent-info-loading">Loading…</div>';
  _infoRawContent = null;

  // show edit button only on context tab (editable markdown files)
  agentInfoEditBtn.style.display = tab === 'context' ? 'flex' : 'none';
  agentInfoEditBtn.classList.remove('editing');
  agentInfoEditBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit';

  if (tab === 'tags')  { renderTagsEditor(name); return; }
  if (tab === 'files') { renderFilesTab(name);  return; }
  if (tab === 'logs')  { renderLogsTab(name);   return; }
  if (tab === 'git')   { renderGitTab(name);    return; }

  // tab → path mapping: context = context.md, others = <tab>/README.md
  var filePath = tab === 'context'
    ? name + '/context.md'
    : name + '/' + tab + '/README.md';

  fetch('/api/browser/file?section=agents&path=' + encodeURIComponent(filePath))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.error) {
        // File missing — show file list for that subdir instead
        agentInfoBody.innerHTML = '<div style="font-size:11px;color:var(--text2);margin-bottom:12px;">' +
          'No README.md found in ' + tab + '/. Files in this directory:</div>' +
          '<div class="info-file-list" id="info-file-list"></div>';
        // List files in the subdir
        fetch('/api/browser/list?section=agents')
          .then(function(r) { return r.json(); })
          .then(function(ld) {
            var prefix = name + '/' + tab + '/';
            var files  = (ld.files || []).filter(function(f) { return f.path.startsWith(prefix); });
            var list   = document.getElementById('info-file-list');
            if (!list) return;
            if (!files.length) {
              list.innerHTML = '<div style="font-size:11px;color:var(--text2);opacity:0.5;">No files yet.</div>';
              return;
            }
            files.forEach(function(f) {
              var parts   = f.path.split('/');
              var fname   = parts[parts.length - 1];
              var ext     = fname.split('.').pop().toLowerCase();
              var icon    = ext === 'md' ? '📄' : ext === 'py' ? '🐍' : ext === 'sh' ? '📜' : '📎';
              var item    = document.createElement('div');
              item.className = 'info-file-item';
              item.innerHTML = '<span>' + icon + '</span>' +
                '<span class="info-file-name">' + escHtml(fname) + '</span>' +
                '<span class="info-file-size">' + (f.size > 1024 ? (f.size/1024).toFixed(1)+'kb' : f.size+'b') + '</span>';
              item.addEventListener('click', function() {
                fetch('/api/browser/file?section=agents&path=' + encodeURIComponent(f.path))
                  .then(function(r) { return r.json(); })
                  .then(function(fd) { renderInfoContent(fd); });
              });
              list.appendChild(item);
            });
          });
        return;
      }
      renderInfoContent(d);
    })
    .catch(function() {
      agentInfoBody.innerHTML = '<div style="color:var(--red);padding:20px;font-size:12px;">Failed to load.</div>';
    });
}

function renderInfoContent(d) {
  _infoRawContent = d.content; // stash for edit mode
  if (d.kind === 'markdown' && window.marked) {
    agentInfoBody.innerHTML = marked.parse(d.content);
  } else {
    var pre  = document.createElement('pre');
    var code = document.createElement('code');
    code.textContent = d.content;
    pre.appendChild(code);
    agentInfoBody.innerHTML = '';
    agentInfoBody.appendChild(pre);
  }
  agentInfoBody.scrollTop = 0;
}

function _enterContextEdit() {
  if (!_infoRawContent) return;
  agentInfoEditBtn.classList.add('editing');
  agentInfoEditBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Save';

  // Replace body with editor
  agentInfoBody.style.cssText = 'display:flex; flex-direction:column; padding:16px 22px; gap:0; overflow:hidden;';
  agentInfoBody.innerHTML = '';

  var ta = document.createElement('textarea');
  ta.id = 'context-editor';
  ta.value = _infoRawContent;
  ta.spellcheck = false;

  var actions = document.createElement('div');
  actions.id = 'context-edit-actions';

  var saveBtn = document.createElement('button');
  saveBtn.id = 'context-save-btn';
  saveBtn.textContent = 'Save';
  saveBtn.onclick = _saveContextEdit;

  var cancelBtn = document.createElement('button');
  cancelBtn.id = 'context-cancel-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.onclick = function() {
    agentInfoEditBtn.classList.remove('editing');
    agentInfoEditBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit';
    agentInfoBody.style.cssText = '';
    loadInfoTab(_infoAgent, _infoTab);
  };

  var status = document.createElement('span');
  status.id = 'context-edit-status';

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);
  actions.appendChild(status);

  agentInfoBody.appendChild(ta);
  agentInfoBody.appendChild(actions);
  ta.focus();
}

function _saveContextEdit() {
  var ta = document.getElementById('context-editor');
  var status = document.getElementById('context-edit-status');
  if (!ta || !_infoAgent) return;

  var filePath = _infoAgent + '/context.md';
  if (status) status.textContent = 'Saving…';
  if (document.getElementById('context-save-btn')) document.getElementById('context-save-btn').disabled = true;

  fetch('/api/browser/file?section=agents&path=' + encodeURIComponent(filePath), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: ta.value })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      _infoRawContent = ta.value;
      agentInfoEditBtn.classList.remove('editing');
      agentInfoEditBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit';
      agentInfoBody.style.cssText = '';
      loadInfoTab(_infoAgent, _infoTab);
    } else {
      if (status) status.textContent = 'Error: ' + (d.error || 'save failed');
      if (document.getElementById('context-save-btn')) document.getElementById('context-save-btn').disabled = false;
    }
  })
  .catch(function() {
    if (status) status.textContent = 'Network error';
    if (document.getElementById('context-save-btn')) document.getElementById('context-save-btn').disabled = false;
  });
}

function closeAgentInfo() {
  if (window._infoTabTimer) { clearInterval(window._infoTabTimer); window._infoTabTimer = null; }
  agentInfoOverlay.classList.remove('open');
  agentInfoEditBtn.style.display = 'none';
  agentInfoEditBtn.classList.remove('editing');
  _infoAgent = null;
  _infoRawContent = null;
}

// ── File Manager tab ────────────────────────────────────────────────────────
function renderFilesTab(name) {
  agentInfoBody.innerHTML = '';
  agentInfoBody.style.cssText = 'padding:0 22px; overflow-y:hidden; display:flex; flex-direction:column; min-height:0;';

  var wrap = document.createElement('div');
  wrap.id = 'fm-wrap';
  wrap.style.cssText = 'display:flex; flex-direction:column; flex:1; min-height:0; overflow:hidden;';

  // toolbar
  var toolbar = document.createElement('div');
  toolbar.id = 'fm-toolbar';

  var uploadBtn = document.createElement('button');
  uploadBtn.id = 'fm-upload-btn';
  uploadBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Upload';

  var uploadInput = document.createElement('input');
  uploadInput.id   = 'fm-upload-input';
  uploadInput.type = 'file';
  uploadInput.multiple = true;

  uploadBtn.addEventListener('click', function() { uploadInput.click(); });
  uploadInput.addEventListener('change', function() {
    var files = Array.from(uploadInput.files);
    if (!files.length) return;
    var fd = new FormData();
    files.forEach(function(f) { fd.append('files', f); });
    uploadBtn.textContent = 'Uploading…';
    uploadBtn.disabled = true;
    fetch('/api/agent/files/upload?agent=' + encodeURIComponent(name), { method: 'POST', body: fd })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        uploadInput.value = '';
        uploadBtn.disabled = false;
        uploadBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg> Upload';
        if (d.error) { toast(d.error); return; }
        toast('Uploaded ' + d.files.length + ' file(s)');
        loadFileList();
      })
      .catch(function() { uploadBtn.disabled = false; toast('Upload failed'); });
  });

  var crumb = document.createElement('div');
  crumb.id = 'fm-path-crumb';
  crumb.textContent = name + '/';

  toolbar.appendChild(uploadBtn);
  toolbar.appendChild(uploadInput);
  toolbar.appendChild(crumb);

  // file list
  var list = document.createElement('div');
  list.id = 'fm-list';

  wrap.appendChild(toolbar);
  wrap.appendChild(list);
  agentInfoBody.appendChild(wrap);

  function fmIcon(fname) {
    var ext = fname.split('.').pop().toLowerCase();
    if (['md','txt'].includes(ext))      return '📄';
    if (['py'].includes(ext))            return '🐍';
    if (['sh','bash'].includes(ext))     return '📜';
    if (['pdf'].includes(ext))           return '📕';
    if (['jpg','jpeg','png','gif','webp'].includes(ext)) return '🖼';
    if (['json'].includes(ext))          return '📋';
    if (['zip','tar','gz'].includes(ext))return '📦';
    return '📎';
  }

  function fmSize(b) {
    if (b > 1048576) return (b/1048576).toFixed(1) + ' MB';
    if (b > 1024)    return (b/1024).toFixed(1) + ' KB';
    return b + ' B';
  }

  function openViewer(relPath, fname) {
    var viewer = document.createElement('div');
    viewer.id = 'fm-viewer';

    var bar = document.createElement('div');
    bar.id = 'fm-viewer-bar';

    var nameEl = document.createElement('div');
    nameEl.id = 'fm-viewer-name';
    nameEl.textContent = fname;

    var dlBtn = document.createElement('button');
    dlBtn.id = 'fm-viewer-dl';
    dlBtn.textContent = 'Download';
    dlBtn.addEventListener('click', function() {
      window.location = '/api/agent/files/download?agent=' + encodeURIComponent(name) +
        '&path=' + encodeURIComponent(relPath);
    });

    var closeBtn = document.createElement('button');
    closeBtn.id = 'fm-viewer-close';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function() { viewer.remove(); });

    bar.appendChild(nameEl);
    bar.appendChild(dlBtn);
    bar.appendChild(closeBtn);

    var body = document.createElement('div');
    body.id = 'fm-viewer-body';
    body.textContent = 'Loading…';

    viewer.appendChild(bar);
    viewer.appendChild(body);
    agentInfoBody.appendChild(viewer);

    fetch('/api/agent/files/download?agent=' + encodeURIComponent(name) +
          '&path=' + encodeURIComponent(relPath) + '&inline=1')
      .then(function(r) { return r.text(); })
      .then(function(txt) {
        var ext = fname.split('.').pop().toLowerCase();
        if (['md'].includes(ext) && window.marked) {
          body.innerHTML = marked.parse(txt);
        } else if (['jpg','jpeg','png','gif','webp'].includes(ext)) {
          body.innerHTML = '<img src="/api/agent/files/download?agent=' +
            encodeURIComponent(name) + '&path=' + encodeURIComponent(relPath) +
            '&inline=1" style="max-width:100%;border-radius:6px;">';
        } else {
          var pre = document.createElement('pre');
          pre.style.cssText = 'font-size:10px;line-height:1.6;white-space:pre-wrap;word-break:break-all;';
          pre.textContent = txt;
          body.innerHTML = '';
          body.appendChild(pre);
        }
      })
      .catch(function() { body.textContent = '(binary file — use Download)'; });
  }

  function loadFileList() {
    list.innerHTML = '<div class="fm-empty">Loading…</div>';
    fetch('/api/agent/files/list?agent=' + encodeURIComponent(name))
      .then(function(r) { return r.json(); })
      .then(function(d) {
        list.innerHTML = '';
        var files = d.files || [];
        if (!files.length) {
          list.innerHTML = '<div class="fm-empty">No files yet. Upload something above.</div>';
          return;
        }
        files.forEach(function(f) {
          var row = document.createElement('div');
          row.className = 'fm-row';

          var icon = document.createElement('div');
          icon.className = 'fm-row-icon';
          icon.textContent = fmIcon(f.name);

          var nameEl = document.createElement('div');
          nameEl.className = 'fm-row-name';
          nameEl.title = f.path;
          nameEl.textContent = f.name;

          var sizeEl = document.createElement('div');
          sizeEl.className = 'fm-row-size';
          sizeEl.textContent = fmSize(f.size);

          var acts = document.createElement('div');
          acts.className = 'fm-row-actions';

          var viewBtn = document.createElement('button');
          viewBtn.className = 'fm-act-btn';
          viewBtn.textContent = 'View';
          viewBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            openViewer(f.path, f.name);
          });

          var dlBtn = document.createElement('button');
          dlBtn.className = 'fm-act-btn fm-act-dl';
          dlBtn.textContent = 'Download';
          dlBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            window.location = '/api/agent/files/download?agent=' + encodeURIComponent(name) +
              '&path=' + encodeURIComponent(f.path);
          });

          acts.appendChild(viewBtn);
          acts.appendChild(dlBtn);
          row.appendChild(icon);
          row.appendChild(nameEl);
          row.appendChild(sizeEl);
          row.appendChild(acts);

          row.addEventListener('click', function() { openViewer(f.path, f.name); });
          list.appendChild(row);
        });
      })
      .catch(function() {
        list.innerHTML = '<div class="fm-empty">Failed to load files.</div>';
      });
  }

  loadFileList();
}

// ── Logs tab (agent info panel) ─────────────────────────────────────────────
function renderLogsTab(name) {
  agentInfoBody.innerHTML = '';
  agentInfoBody.style.cssText = 'padding:0 22px; overflow:hidden; display:flex; flex-direction:column; min-height:0;';

  var wrap = document.createElement('div');
  wrap.id = 'logs-wrap';
  wrap.innerHTML =
    '<div id="logs-toolbar"><div class="logs-empty" style="padding:0;">Discovering log files…</div></div>' +
    '<div id="logs-meta"></div>' +
    '<div id="logs-view"><div class="logs-empty">Loading…</div></div>';
  agentInfoBody.appendChild(wrap);

  var state = {
    files:    [],
    selected: null,    // rel path
    lines:    300,
    auto:     true,
    busy:     false,
    lastSize: 0,
  };

  function fmtSize(b) {
    if (b > 1048576) return (b/1048576).toFixed(1) + ' MB';
    if (b > 1024)    return (b/1024).toFixed(1) + ' KB';
    return b + ' B';
  }

  function fmtAgo(ts) {
    if (!ts) return '';
    var s = Math.max(0, Math.floor(Date.now()/1000) - ts);
    if (s < 60)    return s + 's ago';
    if (s < 3600)  return Math.floor(s/60) + 'm ago';
    if (s < 86400) return Math.floor(s/3600) + 'h ago';
    return Math.floor(s/86400) + 'd ago';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function colorize(line) {
    var esc = escapeHtml(line);
    var lower = line.toLowerCase();
    if (lower.indexOf('error') >= 0 || lower.indexOf('exception') >= 0 || lower.indexOf('traceback') >= 0 || lower.indexOf('failed') >= 0)
      return '<span class="ll-err">' + esc + '</span>';
    if (lower.indexOf('warn') >= 0 || lower.indexOf('401') >= 0 || lower.indexOf('403') >= 0 || lower.indexOf('429') >= 0)
      return '<span class="ll-warn">' + esc + '</span>';
    if (lower.indexOf('dispatched') >= 0 || lower.indexOf('ok') === 0 || lower.indexOf(' 200 ') >= 0 || lower.indexOf('success') >= 0)
      return '<span class="ll-ok">' + esc + '</span>';
    if (line.startsWith('#') || line.startsWith('---') || line.trim() === '')
      return '<span class="ll-dim">' + esc + '</span>';
    return esc;
  }

  function renderToolbar() {
    var tb = document.getElementById('logs-toolbar');
    if (!state.files.length) {
      tb.innerHTML = '<div class="logs-empty" style="padding:0;">No .log files found in agents/' + escapeHtml(name) + '/logs/</div>';
      return;
    }
    var pills = state.files.map(function(f) {
      var cls = 'logs-file-pill' + (f.rel === state.selected ? ' active' : '');
      return '<button class="' + cls + '" data-rel="' + escapeHtml(f.rel) + '">'
        + escapeHtml(f.name)
        + '<span class="logs-file-size">' + fmtSize(f.size) + '</span>'
        + '</button>';
    }).join('');
    tb.innerHTML = pills
      + '<span id="logs-toolbar-spacer"></span>'
      + '<select id="logs-lines-select" title="Lines to tail">'
      +   '<option value="100">100</option>'
      +   '<option value="300" selected>300</option>'
      +   '<option value="1000">1000</option>'
      +   '<option value="5000">5000</option>'
      + '</select>'
      + '<button class="logs-mini-btn ' + (state.auto ? 'on' : '') + '" id="logs-auto-btn" title="Auto-refresh every 3s">'
      +   (state.auto ? 'Auto ON' : 'Auto OFF')
      + '</button>'
      + '<button class="logs-mini-btn" id="logs-refresh-btn">Refresh</button>'
      + '<button class="logs-mini-btn" id="logs-copy-btn">Copy</button>';

    tb.querySelectorAll('.logs-file-pill').forEach(function(btn) {
      btn.addEventListener('click', function() {
        state.selected = this.dataset.rel;
        renderToolbar();
        loadTail(true);
      });
    });
    var sel = document.getElementById('logs-lines-select');
    sel.value = String(state.lines);
    sel.addEventListener('change', function() {
      state.lines = parseInt(sel.value, 10) || 300;
      loadTail(true);
    });
    document.getElementById('logs-auto-btn').addEventListener('click', function() {
      state.auto = !state.auto;
      renderToolbar();
      schedulePolling();
    });
    document.getElementById('logs-refresh-btn').addEventListener('click', function() { loadTail(true); });
    document.getElementById('logs-copy-btn').addEventListener('click', function() {
      var view = document.getElementById('logs-view');
      var txt = view ? view.innerText : '';
      navigator.clipboard.writeText(txt).then(
        function() { toast('Copied ' + txt.length + ' chars'); },
        function() { toast('Copy failed'); }
      );
    });
  }

  function renderTail(d) {
    var view = document.getElementById('logs-view');
    var meta = document.getElementById('logs-meta');
    if (!view || !meta) return;

    var f = state.files.find(function(x) { return x.rel === state.selected; }) || {};
    meta.innerHTML =
      '<span><b>File:</b> ' + escapeHtml(d.rel || state.selected) + '</span>'
      + '<span><b>Size:</b> ' + fmtSize(d.size != null ? d.size : f.size || 0) + '</span>'
      + '<span><b>Showing:</b> last ' + (d.returned_lines || 0) + (d.truncated_head ? ' (truncated)' : '') + '</span>'
      + '<span><b>Updated:</b> ' + (f.mtime ? fmtAgo(f.mtime) : 'just now') + '</span>';

    var atBottom = (view.scrollTop + view.clientHeight + 6) >= view.scrollHeight;
    var lines = d.lines || [];
    if (!lines.length) {
      view.innerHTML = '<div class="logs-empty">File is empty.</div>';
      return;
    }
    view.innerHTML = lines.map(colorize).join('\n');
    if (atBottom || state.lastSize === 0) {
      view.scrollTop = view.scrollHeight;
    }
    state.lastSize = d.size || 0;
  }

  function loadTail(showLoading) {
    if (!state.selected) return;
    if (state.busy) return;
    state.busy = true;
    if (showLoading) {
      document.getElementById('logs-view').innerHTML = '<div class="logs-empty">Loading…</div>';
    }
    fetch('/api/agent/logs/tail?agent=' + encodeURIComponent(name)
        + '&file=' + encodeURIComponent(state.selected)
        + '&lines=' + state.lines)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        state.busy = false;
        if (d.error) {
          document.getElementById('logs-view').innerHTML =
            '<div class="logs-empty" style="color:#f87171;">' + escapeHtml(d.error) + '</div>';
          return;
        }
        // refresh size in our file list so the meta row stays accurate
        var f = state.files.find(function(x) { return x.rel === state.selected; });
        if (f) { f.size = d.size; f.mtime = Math.floor(Date.now()/1000); }
        renderTail(d);
      })
      .catch(function() {
        state.busy = false;
        document.getElementById('logs-view').innerHTML =
          '<div class="logs-empty" style="color:#f87171;">Network error.</div>';
      });
  }

  function schedulePolling() {
    if (window._infoTabTimer) { clearInterval(window._infoTabTimer); window._infoTabTimer = null; }
    if (state.auto) {
      window._infoTabTimer = setInterval(function() { loadTail(false); }, 3000);
    }
  }

  // initial: list logs, then auto-select the most recently modified file
  fetch('/api/agent/logs/list?agent=' + encodeURIComponent(name))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.error) {
        wrap.innerHTML = '<div class="logs-empty" style="color:#f87171;">' + escapeHtml(d.error) + '</div>';
        return;
      }
      state.files = (d.logs || []).slice().sort(function(a, b) { return (b.mtime || 0) - (a.mtime || 0); });
      if (!state.files.length) {
        wrap.innerHTML = '<div class="logs-empty">No .log files found in agents/' + escapeHtml(name) + '/logs/</div>';
        return;
      }
      state.selected = state.files[0].rel;
      renderToolbar();
      loadTail(true);
      schedulePolling();
    })
    .catch(function() {
      wrap.innerHTML = '<div class="logs-empty" style="color:#f87171;">Failed to list logs.</div>';
    });
}

// ── Git tab (agent info panel) ──────────────────────────────────────────────
function renderGitTab(name) {
  agentInfoBody.innerHTML = '';
  agentInfoBody.style.cssText = 'padding:0 22px; overflow:hidden; display:flex; flex-direction:column; min-height:0;';

  var wrap = document.createElement('div');
  wrap.id = 'git-wrap';
  agentInfoBody.appendChild(wrap);

  function setLoading() {
    wrap.innerHTML = '<div class="git-empty">Loading git status from ' + name + '…</div>';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }

  function renderError(d) {
    var html = '<div class="git-card">'
      + '<div class="git-card-title">Remote — ' + escapeHtml(d.host || name) + '</div>'
      + '<span class="git-status-pill git-status-error">Not detected</span>'
      + '<div style="margin-top:10px;font-size:11px;color:var(--text2);line-height:1.6;">'
      + escapeHtml(d.error || 'Could not contact remote.') + '</div>';
    if (d.hint) html += '<div style="margin-top:8px;font-size:10.5px;color:var(--text2);opacity:0.85;">' + escapeHtml(d.hint) + '</div>';
    html += '<div class="git-actions" style="margin-top:12px;">'
      + '<button class="git-btn" id="git-redetect">Re-detect</button>'
      + '<input id="git-manual-path" placeholder="or paste remote path: ~/public_html" />'
      + '<button class="git-btn" id="git-manual-set">Set path</button>'
      + '</div></div>';
    wrap.innerHTML = html;

    document.getElementById('git-redetect').addEventListener('click', function() {
      this.disabled = true; this.textContent = 'Detecting…';
      fetch('/api/agent/git/detect?agent=' + encodeURIComponent(name), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      }).then(function(r){return r.json();}).then(function(){ load(); });
    });
    document.getElementById('git-manual-set').addEventListener('click', function() {
      var path = document.getElementById('git-manual-path').value.trim();
      if (!path) return;
      this.disabled = true; this.textContent = 'Setting…';
      fetch('/api/agent/git/detect?agent=' + encodeURIComponent(name), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: path})
      }).then(function(r){return r.json();}).then(function(d){
        if (!d.ok) { toast(d.error || 'Path not a git repo'); load(); return; }
        load();
      });
    });
  }

  function flagClass(flag) {
    if (!flag) return '';
    var f = flag.trim();
    if (f === '??') return 'QQ';
    var c = f.charAt(0);
    if ('MADRU'.indexOf(c) >= 0) return c;
    return '';
  }

  function renderStatus(d) {
    var commits = d.commits || [];
    var changes = d.changes || [];
    var pillCls = d.dirty ? 'git-status-dirty' : 'git-status-clean';
    var pillTxt = d.dirty ? (changes.length + ' uncommitted') : 'Clean';
    var upstreamLine = d.upstream
      ? '<span><b>Upstream:</b> <code>' + escapeHtml(d.upstream) + '</code></span>'
      : '<span style="color:#facc15;"><b>Upstream:</b> none (push will fail until set)</span>';

    var html = '';

    // Header card
    html += '<div class="git-card">'
      + '<div class="git-card-title">Remote</div>'
      + '<div class="git-meta-row">'
      +   '<span><b>Host:</b> <code>' + escapeHtml(d.host) + '</code></span>'
      +   '<span><b>Path:</b> <code>' + escapeHtml(d.path) + '</code></span>'
      +   '<span><b>Branch:</b> <code>' + escapeHtml(d.branch || '?') + '</code></span>'
      + '</div>'
      + '<div class="git-meta-row" style="margin-top:6px;">'
      +   '<span><b>Origin:</b> ' + (d.remote ? '<code>' + escapeHtml(d.remote) + '</code>' : '<span style="color:#f87171;">none</span>') + '</span>'
      +   upstreamLine
      + '</div>'
      + '<div class="git-actions" style="margin-top:10px;">'
      +   '<span class="git-status-pill ' + pillCls + '">' + pillTxt + '</span>'
      +   '<button class="git-btn" id="git-refresh">Refresh</button>'
      +   '<button class="git-btn" id="git-redetect-mini">Re-detect path</button>'
      + '</div>'
      + '</div>';

    // Status / changes card
    if (changes.length) {
      var rows = changes.map(function(line) {
        var flag = line.substring(0, 2);
        var rest = line.substring(3);
        return '<div class="git-change-row">'
          + '<span class="git-change-flag ' + flagClass(flag) + '">' + escapeHtml(flag) + '</span>'
          + '<span class="git-change-name">' + escapeHtml(rest) + '</span>'
          + '</div>';
      }).join('');
      html += '<div class="git-card">'
        + '<div class="git-card-title">Uncommitted changes (' + changes.length + ')</div>'
        + '<div class="git-changes-list">' + rows + '</div>'
        + '</div>';
    }

    // Commit + push card
    var pushDisabled = !d.dirty;
    var defaultMsg = d.dirty ? 'Update from JARVIS dashboard — ' + new Date().toISOString().slice(0,16).replace('T',' ') + ' UTC' : '';
    html += '<div class="git-card">'
      + '<div class="git-card-title">Commit & Push</div>'
      + '<textarea id="git-commit-msg" placeholder="' + (d.dirty ? 'Commit message' : 'Nothing to commit') + '">' + escapeHtml(defaultMsg) + '</textarea>'
      + '<div class="git-actions">'
      +   '<button class="git-btn git-btn-primary" id="git-push-btn"' + (pushDisabled ? ' disabled' : '') + '>'
      +     '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg>'
      +     'Add, Commit & Push'
      +   '</button>'
      +   (pushDisabled ? '<span style="font-size:10.5px;color:var(--text2);">Working tree is clean.</span>' : '')
      + '</div>'
      + '<div id="git-push-output" style="display:none;"></div>'
      + '</div>';

    // Recent commits
    if (commits.length) {
      var rows2 = commits.map(function(c) {
        return '<div class="git-commit-row">'
          + '<span class="git-commit-sha">' + escapeHtml(c.sha) + '</span>'
          + '<span class="git-commit-subj">' + escapeHtml(c.subject) + '</span>'
          + '<span class="git-commit-meta">' + escapeHtml(c.author) + ' · ' + escapeHtml(c.when) + '</span>'
          + '</div>';
      }).join('');
      html += '<div class="git-card">'
        + '<div class="git-card-title">Recent commits</div>'
        + '<div class="git-commit-list">' + rows2 + '</div>'
        + '</div>';
    } else {
      html += '<div class="git-card">'
        + '<div class="git-card-title">Recent commits</div>'
        + '<div class="git-empty">No commits yet.</div>'
        + '</div>';
    }

    wrap.innerHTML = html;

    document.getElementById('git-refresh').addEventListener('click', load);
    document.getElementById('git-redetect-mini').addEventListener('click', function() {
      this.disabled = true;
      fetch('/api/agent/git/detect?agent=' + encodeURIComponent(name), {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      }).then(function(r){return r.json();}).then(function(d){
        if (d.ok) toast('Re-detected: ' + d.path);
        else toast(d.error || 'Re-detect failed');
        load();
      });
    });

    var pushBtn = document.getElementById('git-push-btn');
    if (pushBtn) {
      pushBtn.addEventListener('click', function() {
        var msg = document.getElementById('git-commit-msg').value.trim();
        if (!msg) { toast('Commit message required'); return; }
        pushBtn.disabled = true;
        pushBtn.innerHTML = 'Pushing…';
        var out = document.getElementById('git-push-output');
        out.style.display = 'block';
        out.className = 'git-output';
        out.textContent = 'Running on remote: git add -A && git commit && git push …';
        fetch('/api/agent/git/push?agent=' + encodeURIComponent(name), {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({message: msg})
        }).then(function(r){return r.json();}).then(function(d){
          out.textContent = d.output || (d.ok ? 'Done.' : 'Failed.');
          if (d.ok) {
            toast(d.no_changes ? 'Nothing to commit' : 'Pushed successfully');
            setTimeout(load, 600);
          } else {
            toast('Push failed (rc=' + d.rc + ')');
            pushBtn.disabled = false;
            pushBtn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg> Add, Commit & Push';
          }
        }).catch(function() {
          out.textContent = 'Network error.';
          pushBtn.disabled = false;
          pushBtn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7"/></svg> Add, Commit & Push';
        });
      });
    }
  }

  function load() {
    setLoading();
    fetch('/api/agent/git/status?agent=' + encodeURIComponent(name))
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.error || d.ok === false) { renderError(d); return; }
        renderStatus(d);
      })
      .catch(function(e) {
        renderError({error: 'Network error: ' + (e && e.message || e)});
      });
  }

  load();
}

// ── Tags editor (agent info panel) ──────────────────────────────────────────
function renderTagsEditor(name) {
  // Tab now renders Agent Settings: Model picker + Tags editor + Auto-Hibernate.
  // All endpoints load in parallel so we draw once with full state.
  Promise.all([
    fetch('/api/agent/tags/'  + encodeURIComponent(name)).then(function(r){ return r.json(); }),
    fetch('/api/agent/model/' + encodeURIComponent(name)).then(function(r){ return r.json(); }),
    fetch('/api/agent/hibernation').then(function(r){ return r.json(); }),
  ]).then(function(results) {
      var d = results[0] || {};
      var m = results[1] || {};
      var hib = results[2] || {agents:{}, settings:{}};
      var tags = d.tags || [];
      agentInfoBody.innerHTML = '';

      var wrap = document.createElement('div');
      wrap.style.cssText = 'padding:20px;display:flex;flex-direction:column;gap:22px;';

      // ── Model section ──────────────────────────────────────────────
      var modelSec = document.createElement('div');
      modelSec.style.cssText = 'display:flex;flex-direction:column;gap:10px;';

      var mh = document.createElement('div');
      mh.innerHTML = '<span style="font-size:13px;font-weight:700;color:var(--text)">Cursor Agent Model</span>' +
        '<span style="font-size:11px;color:var(--text2);margin-left:8px;">applied on next agent start</span>';
      modelSec.appendChild(mh);

      var mRow = document.createElement('div');
      mRow.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;';

      var sel = document.createElement('select');
      sel.style.cssText = 'flex:1;min-width:240px;background:rgba(0,0,0,0.3);border:1px solid var(--border);' +
        'border-radius:7px;padding:7px 10px;color:var(--text);font-size:12px;outline:none;';
      var current = m.model || (m.default || 'composer-2.5');
      var choices = Array.isArray(m.choices) && m.choices.length ? m.choices
                    : [{slug: current, label: current}];
      // Make sure the currently-set slug is always present even if not in the
      // curated choices list (e.g. someone hand-edited .cursor-model).
      if (!choices.some(function(c){ return c.slug === current; })) {
        choices = [{slug: current, label: current + ' (custom)'}].concat(choices);
      }
      choices.forEach(function(c) {
        var o = document.createElement('option');
        o.value = c.slug;
        o.textContent = c.label;
        if (c.slug === current) o.selected = true;
        sel.appendChild(o);
      });
      mRow.appendChild(sel);

      var saveOnlyBtn = document.createElement('button');
      saveOnlyBtn.textContent = 'Save';
      saveOnlyBtn.title = 'Save model preference (takes effect on next start)';
      saveOnlyBtn.style.cssText = 'font-size:12px;font-weight:700;padding:7px 14px;border-radius:7px;' +
        'background:rgba(255,255,255,0.06);color:var(--text);border:1px solid var(--border);cursor:pointer;';

      var saveRestartBtn = document.createElement('button');
      saveRestartBtn.textContent = 'Save & Restart';
      saveRestartBtn.title = 'Save and restart the agent so pane 1 reloads with the new model';
      saveRestartBtn.style.cssText = 'font-size:12px;font-weight:700;padding:7px 14px;border-radius:7px;' +
        'background:var(--accent);color:#000;border:none;cursor:pointer;';

      function postModel() {
        return fetch('/api/agent/model/' + encodeURIComponent(name), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: sel.value })
        }).then(function(r) { return r.json().then(function(j){ return {ok: r.ok, body: j}; }); });
      }

      var statusEl = document.createElement('span');
      statusEl.style.cssText = 'font-size:11px;color:var(--text2);';

      saveOnlyBtn.addEventListener('click', function() {
        statusEl.textContent = 'Saving…';
        postModel().then(function(res) {
          if (!res.ok) { statusEl.textContent = '✗ ' + (res.body.error || 'failed'); statusEl.style.color = 'var(--red)'; return; }
          statusEl.textContent = '✓ saved (restart to apply)';
          statusEl.style.color = 'var(--green)';
        });
      });

      saveRestartBtn.addEventListener('click', function() {
        if (!confirm('Save model and restart ' + name + '? This will stop and start the agent session.')) return;
        statusEl.textContent = 'Saving…';
        postModel().then(function(res) {
          if (!res.ok) { statusEl.textContent = '✗ ' + (res.body.error || 'failed'); statusEl.style.color = 'var(--red)'; return; }
          statusEl.textContent = 'Stopping…';
          statusEl.style.color = 'var(--text2)';
          return fetch('/api/stop', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({name: name})
          }).then(function(){
            statusEl.textContent = 'Starting…';
            return new Promise(function(r){ setTimeout(r, 1500); });
          }).then(function(){
            return fetch('/api/start', {
              method: 'POST', headers: {'Content-Type':'application/json'},
              body: JSON.stringify({name: name})
            });
          }).then(function(){
            statusEl.textContent = '✓ restarted with ' + sel.value;
            statusEl.style.color = 'var(--green)';
            if (typeof refreshAgents === 'function') setTimeout(refreshAgents, 2000);
          });
        });
      });

      mRow.appendChild(saveOnlyBtn);
      mRow.appendChild(saveRestartBtn);
      modelSec.appendChild(mRow);
      modelSec.appendChild(statusEl);
      wrap.appendChild(modelSec);

      // divider
      var hr = document.createElement('div');
      hr.style.cssText = 'height:1px;background:rgba(255,255,255,0.06);margin:2px 0;';
      wrap.appendChild(hr);

      // heading
      var h = document.createElement('div');
      h.innerHTML = '<span style="font-size:13px;font-weight:700;color:var(--text)">Tags</span>' +
        '<span style="font-size:11px;color:var(--text2);margin-left:8px;">shown in sub-nav for filtering</span>';
      wrap.appendChild(h);

      // existing tags
      var chipsWrap = document.createElement('div');
      chipsWrap.id = 'tag-chips-wrap';
      chipsWrap.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;min-height:28px;';
      function renderChips(t) {
        chipsWrap.innerHTML = '';
        if (!t.length) {
          chipsWrap.innerHTML = '<span style="font-size:11px;color:var(--text2);opacity:0.5;">No tags yet.</span>';
          return;
        }
        t.forEach(function(tag) {
          var chip = document.createElement('span');
          chip.style.cssText = 'display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;' +
            'padding:3px 10px;border-radius:16px;background:rgba(56,189,248,0.12);' +
            'border:1px solid rgba(56,189,248,0.35);color:var(--accent);cursor:default;';
          chip.innerHTML = escHtml(tag) +
            '<span style="cursor:pointer;opacity:0.6;font-size:13px;line-height:1;" data-rm="' + escHtml(tag) + '">&times;</span>';
          chip.querySelector('[data-rm]').addEventListener('click', function() {
            tags = tags.filter(function(x) { return x !== tag; });
            renderChips(tags);
            saveTags(name, tags);
          });
          chipsWrap.appendChild(chip);
        });
      }
      renderChips(tags);
      wrap.appendChild(chipsWrap);

      // add input
      var row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:8px;';
      var inp = document.createElement('input');
      inp.type = 'text';
      inp.placeholder = 'e.g. client, active, dev…';
      inp.style.cssText = 'flex:1;background:rgba(0,0,0,0.3);border:1px solid var(--border);' +
        'border-radius:7px;padding:7px 12px;color:var(--text);font-size:12px;outline:none;';
      var addBtn = document.createElement('button');
      addBtn.textContent = 'Add';
      addBtn.style.cssText = 'font-size:12px;font-weight:700;padding:7px 16px;border-radius:7px;' +
        'background:var(--accent);color:#000;border:none;cursor:pointer;';
      function doAdd() {
        var val = inp.value.trim().toLowerCase();
        if (!val || tags.includes(val)) { inp.value = ''; return; }
        tags.push(val);
        tags.sort();
        inp.value = '';
        renderChips(tags);
        saveTags(name, tags);
      }
      addBtn.addEventListener('click', doAdd);
      inp.addEventListener('keydown', function(e) { if (e.key === 'Enter') doAdd(); });
      row.appendChild(inp);
      row.appendChild(addBtn);
      wrap.appendChild(row);

      // ── Auto-Hibernate section (per-agent) ─────────────────────────
      var hibAgent    = (hib.agents && hib.agents[name]) || {};
      var hibSettings = hib.settings || {};
      var hibCount    = hib.hibernated_count || 0;

      var hibHr = document.createElement('div');
      hibHr.style.cssText = 'height:1px;background:rgba(255,255,255,0.06);margin:2px 0;';
      wrap.appendChild(hibHr);

      var hibHead = document.createElement('div');
      hibHead.innerHTML = '<span style="font-size:13px;font-weight:700;color:var(--text)">Auto-Hibernate</span>' +
        '<span style="font-size:11px;color:var(--text2);margin-left:8px;">free RAM when this agent is idle &mdash; the dashboard wakes it on the next RC message</span>';
      wrap.appendChild(hibHead);

      // Live status strip
      var statusRow = document.createElement('div');
      statusRow.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--text2);';

      var statePill = document.createElement('span');
      var st = hibAgent.status || 'running';
      var pillBg = '#3fb950'; var pillFg = '#fff'; var pillTxt = st;
      if (hibAgent.disabled)         { pillBg = '#475569'; pillTxt = 'disabled'; }
      else if (hibAgent.always_on)   { pillBg = '#10b981'; pillTxt = st + ' · always-on'; }
      else if (st === 'hibernated')  { pillBg = '#60a5fa'; }
      else if (st === 'waking')      { pillBg = '#fbbf24'; pillFg = '#000'; }
      statePill.style.cssText = 'padding:2px 9px;border-radius:10px;font-size:10px;font-weight:700;letter-spacing:0.4px;text-transform:uppercase;background:' + pillBg + ';color:' + pillFg + ';';
      statePill.textContent = pillTxt;
      statusRow.appendChild(statePill);

      var idleSpan = document.createElement('span');
      var im = hibAgent.idle_minutes;
      if (typeof im === 'number' && im >= 0) {
        var hrs = im / 60;
        var idleStr = hrs >= 1 ? hrs.toFixed(1) + 'h idle' : im + 'm idle';
        idleSpan.textContent = idleStr;
      } else {
        idleSpan.textContent = 'no log activity yet';
      }
      statusRow.appendChild(idleSpan);

      if (hibAgent.hibernated_at) {
        var sinceSpan = document.createElement('span');
        sinceSpan.style.opacity = '0.65';
        sinceSpan.textContent = '· slept since ' + new Date(hibAgent.hibernated_at).toLocaleString();
        statusRow.appendChild(sinceSpan);
      }
      if (hibAgent.wake_count_today) {
        var wcSpan = document.createElement('span');
        wcSpan.style.opacity = '0.65';
        wcSpan.textContent = '· ' + hibAgent.wake_count_today + ' wake' + (hibAgent.wake_count_today > 1 ? 's' : '') + ' today';
        statusRow.appendChild(wcSpan);
      }
      wrap.appendChild(statusRow);

      // ── Mode selector: Auto / Always-on / Disabled ────────────────
      // Three-way radio replaces the old always_on checkbox so the
      // mutual exclusion is obvious in the UI.
      var modeWrap = document.createElement('div');
      modeWrap.style.cssText = 'display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--text);';
      var modeHead = document.createElement('span');
      modeHead.style.cssText = 'font-size:11px;font-weight:700;color:var(--text2);';
      modeHead.textContent = 'Mode';
      modeWrap.appendChild(modeHead);

      var modeOptionsRow = document.createElement('div');
      modeOptionsRow.style.cssText = 'display:flex;flex-direction:column;gap:4px;padding-left:2px;';

      var currentMode = hibAgent.disabled  ? 'disabled'
                      : hibAgent.always_on ? 'always_on'
                      : 'auto';

      var modes = [
        {val: 'auto',      label: 'Auto',      desc: 'sleep when idle, wake on next RC message (default)'},
        {val: 'always_on', label: 'Always-on', desc: 'never auto-hibernate (skip the watcher)'},
        {val: 'disabled',  label: 'Disabled',  desc: 'force off &mdash; sleep on next tick and never auto-wake. Manual Wake-now still works.'}
      ];

      var radioName = 'hib-mode-' + name.replace(/[^a-zA-Z0-9]/g, '_');
      var radios = {};
      modes.forEach(function(m) {
        var lbl = document.createElement('label');
        lbl.style.cssText = 'display:flex;align-items:flex-start;gap:8px;cursor:pointer;user-select:none;line-height:1.4;';
        var r = document.createElement('input');
        r.type = 'radio'; r.name = radioName; r.value = m.val;
        r.checked = (currentMode === m.val);
        r.style.cssText = 'margin-top:2px;cursor:pointer;accent-color:' +
          (m.val === 'disabled' ? '#475569' : m.val === 'always_on' ? 'var(--accent)' : '#60a5fa') + ';';
        radios[m.val] = r;
        lbl.appendChild(r);
        var txt = document.createElement('span');
        txt.innerHTML = '<b>' + m.label + '</b> <span style="color:var(--text2)">&mdash; ' + m.desc + '</span>';
        lbl.appendChild(txt);
        modeOptionsRow.appendChild(lbl);
      });
      modeWrap.appendChild(modeOptionsRow);
      wrap.appendChild(modeWrap);

      var hibStatus = document.createElement('div');
      hibStatus.style.cssText = 'font-size:11px;color:var(--text2);min-height:14px;';
      wrap.appendChild(hibStatus);

      Object.keys(radios).forEach(function(modeVal) {
        radios[modeVal].addEventListener('change', function() {
          if (!radios[modeVal].checked) return;
          hibStatus.textContent = 'Saving…';
          hibStatus.style.color = 'var(--text2)';
          var prevMode = currentMode;
          fetch('/api/agent/hibernation/' + encodeURIComponent(name), {
            method: 'PATCH',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({mode: modeVal})
          }).then(function(r){ return r.json(); }).then(function(j) {
            if (j.ok) {
              currentMode = modeVal;
              var msg = {
                'auto':      '✓ auto-hibernate enabled',
                'always_on': '✓ always-on (auto-hibernate skipped)',
                'disabled':  '✓ disabled — agent will sleep on next tick and stay off'
              }[modeVal] || '✓ saved';
              hibStatus.textContent = msg;
              hibStatus.style.color = 'var(--green)';
              if (typeof refreshAgents === 'function') setTimeout(refreshAgents, 1500);
            } else {
              hibStatus.textContent = '✗ ' + (j.error || 'failed');
              hibStatus.style.color = 'var(--red)';
              if (radios[prevMode]) radios[prevMode].checked = true;
            }
          }).catch(function() {
            hibStatus.textContent = '✗ network error';
            hibStatus.style.color = 'var(--red)';
            if (radios[prevMode]) radios[prevMode].checked = true;
          });
        });
      });

      // Manual hibernate / wake buttons
      var btnRow = document.createElement('div');
      btnRow.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';

      var hibNow = document.createElement('button');
      hibNow.textContent = 'Hibernate now';
      hibNow.title = 'Kill the tmux session right now. Dashboard will still wake it on next RC message.';
      hibNow.style.cssText = 'font-size:12px;font-weight:700;padding:7px 14px;border-radius:7px;' +
        'background:rgba(96,165,250,0.15);color:#60a5fa;border:1px solid rgba(96,165,250,0.4);cursor:pointer;';

      var wakeNow = document.createElement('button');
      wakeNow.textContent = 'Wake now';
      wakeNow.title = 'Force-deploy this agent now (skips the ack message).';
      wakeNow.style.cssText = 'font-size:12px;font-weight:700;padding:7px 14px;border-radius:7px;' +
        'background:rgba(251,191,36,0.15);color:#fbbf24;border:1px solid rgba(251,191,36,0.4);cursor:pointer;';

      btnRow.appendChild(hibNow);
      btnRow.appendChild(wakeNow);
      wrap.appendChild(btnRow);

      function doManual(action, btn, originalLabel) {
        if (action === 'hibernate' && !confirm('Hibernate ' + name + '? This kills the tmux session immediately.')) return;
        btn.disabled = true;
        var original = btn.textContent;
        btn.textContent = action === 'hibernate' ? 'Hibernating…' : 'Waking… (deploy can take 30s)';
        hibStatus.textContent = '';
        fetch('/api/agent/hibernation/' + encodeURIComponent(name) + '/' + action, {
          method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}'
        }).then(function(r){ return r.json(); }).then(function(j) {
          btn.disabled = false;
          btn.textContent = original;
          if (j.ok) {
            hibStatus.textContent = '✓ ' + (action === 'hibernate' ? 'hibernated' : 'woken') + ' — refreshing tile…';
            hibStatus.style.color = 'var(--green)';
            if (typeof refreshAgents === 'function') setTimeout(refreshAgents, 1500);
          } else {
            hibStatus.textContent = '✗ ' + (j.error || 'failed');
            hibStatus.style.color = 'var(--red)';
          }
        }).catch(function() {
          btn.disabled = false;
          btn.textContent = original;
          hibStatus.textContent = '✗ network error';
          hibStatus.style.color = 'var(--red)';
        });
      }
      hibNow.addEventListener('click', function() { doManual('hibernate', hibNow); });
      wakeNow.addEventListener('click', function() { doManual('wake', wakeNow); });

      // ── Global Auto-Hibernate settings (collapsible) ───────────────
      var globalDetails = document.createElement('details');
      globalDetails.style.cssText = 'margin-top:6px;border:1px solid var(--border);border-radius:7px;padding:8px 12px;background:rgba(255,255,255,0.02);';

      var summary = document.createElement('summary');
      summary.style.cssText = 'cursor:pointer;font-size:12px;font-weight:600;color:var(--text);user-select:none;';
      summary.innerHTML = 'Global Auto-Hibernate settings ' +
        '<span style="color:var(--text2);font-weight:500;">— ' +
        (hibSettings.enabled ? 'enabled' : 'paused') +
        ', idle ' + (hibSettings.idle_hours || 24) + 'h, ' +
        '<span style="color:#60a5fa;font-weight:700">' + hibCount + '</span> sleeping</span>';
      globalDetails.appendChild(summary);

      var gWrap = document.createElement('div');
      gWrap.style.cssText = 'display:flex;flex-direction:column;gap:10px;margin-top:10px;font-size:12px;color:var(--text);';

      var gEnabledRow = document.createElement('label');
      gEnabledRow.style.cssText = 'display:flex;align-items:center;gap:8px;cursor:pointer;';
      var gEnabledChk = document.createElement('input');
      gEnabledChk.type = 'checkbox';
      gEnabledChk.checked = !!hibSettings.enabled;
      gEnabledChk.style.cssText = 'width:14px;height:14px;cursor:pointer;accent-color:var(--accent);';
      gEnabledRow.appendChild(gEnabledChk);
      var gEnabledLbl = document.createElement('span');
      gEnabledLbl.innerHTML = '<b>Auto-hibernate enabled</b> — uncheck to pause the watcher';
      gEnabledRow.appendChild(gEnabledLbl);
      gWrap.appendChild(gEnabledRow);

      var gIdleRow = document.createElement('div');
      gIdleRow.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;';
      var gIdleLbl = document.createElement('span'); gIdleLbl.textContent = 'Idle threshold:';
      gIdleRow.appendChild(gIdleLbl);
      var gIdleInp = document.createElement('input');
      gIdleInp.type = 'number'; gIdleInp.min = '0.05'; gIdleInp.step = '0.5';
      gIdleInp.value = hibSettings.idle_hours || 24;
      gIdleInp.style.cssText = 'width:80px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;padding:5px 8px;color:var(--text);font-size:12px;outline:none;';
      gIdleRow.appendChild(gIdleInp);
      var gIdleUnit = document.createElement('span'); gIdleUnit.textContent = 'hours';
      gIdleUnit.style.color = 'var(--text2)';
      gIdleRow.appendChild(gIdleUnit);
      gWrap.appendChild(gIdleRow);

      var gAckRow = document.createElement('div');
      gAckRow.style.cssText = 'display:flex;flex-direction:column;gap:5px;';
      var gAckLbl = document.createElement('span');
      gAckLbl.textContent = 'Ack message (posted to channel before redeploy):';
      gAckLbl.style.color = 'var(--text2)'; gAckLbl.style.fontSize = '11px';
      gAckRow.appendChild(gAckLbl);
      var gAckInp = document.createElement('input');
      gAckInp.type = 'text';
      gAckInp.value = hibSettings.ack_message || 'Waking up, one sec...';
      gAckInp.style.cssText = 'background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px;outline:none;';
      gAckRow.appendChild(gAckInp);
      gWrap.appendChild(gAckRow);

      var gReadyRow = document.createElement('div');
      gReadyRow.style.cssText = 'display:flex;flex-direction:column;gap:5px;';
      var gReadyLbl = document.createElement('span');
      gReadyLbl.textContent = 'Ready message (posted after deploy completes — leave blank to skip):';
      gReadyLbl.style.color = 'var(--text2)'; gReadyLbl.style.fontSize = '11px';
      gReadyRow.appendChild(gReadyLbl);
      var gReadyInp = document.createElement('input');
      gReadyInp.type = 'text';
      gReadyInp.value = hibSettings.ready_message != null ? hibSettings.ready_message : '✓ Ready — what can I help with?';
      gReadyInp.style.cssText = 'background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px;outline:none;';
      gReadyRow.appendChild(gReadyInp);
      gWrap.appendChild(gReadyRow);

      var gGraceRow = document.createElement('div');
      gGraceRow.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;';
      var gGraceLbl = document.createElement('span');
      gGraceLbl.textContent = 'Post-wake grace:';
      gGraceLbl.style.color = 'var(--text2)'; gGraceLbl.style.fontSize = '11px';
      gGraceRow.appendChild(gGraceLbl);
      var gGraceInp = document.createElement('input');
      gGraceInp.type = 'number'; gGraceInp.min = '0'; gGraceInp.step = '1';
      gGraceInp.value = hibSettings.post_wake_grace_min != null ? hibSettings.post_wake_grace_min : 10;
      gGraceInp.style.cssText = 'width:70px;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;padding:5px 8px;color:var(--text);font-size:12px;outline:none;';
      gGraceRow.appendChild(gGraceInp);
      var gGraceUnit = document.createElement('span');
      gGraceUnit.textContent = 'minutes (min time before a freshly-woken agent can re-hibernate)';
      gGraceUnit.style.color = 'var(--text2)'; gGraceUnit.style.fontSize = '11px';
      gGraceRow.appendChild(gGraceUnit);
      gWrap.appendChild(gGraceRow);

      var gBtnRow = document.createElement('div');
      gBtnRow.style.cssText = 'display:flex;gap:8px;align-items:center;';
      var gSaveBtn = document.createElement('button');
      gSaveBtn.textContent = 'Save global settings';
      gSaveBtn.style.cssText = 'font-size:12px;font-weight:700;padding:7px 14px;border-radius:7px;background:var(--accent);color:#000;border:none;cursor:pointer;';
      var gStatus = document.createElement('span');
      gStatus.style.cssText = 'font-size:11px;color:var(--text2);';
      gBtnRow.appendChild(gSaveBtn);
      gBtnRow.appendChild(gStatus);
      gWrap.appendChild(gBtnRow);

      gSaveBtn.addEventListener('click', function() {
        var payload = {
          enabled:             gEnabledChk.checked,
          idle_hours:          parseFloat(gIdleInp.value),
          ack_message:         gAckInp.value,
          ready_message:       gReadyInp.value,
          post_wake_grace_min: parseInt(gGraceInp.value, 10) || 0
        };
        if (!isFinite(payload.idle_hours) || payload.idle_hours < 0.05) {
          gStatus.textContent = '✗ idle_hours must be ≥ 0.05';
          gStatus.style.color = 'var(--red)';
          return;
        }
        gStatus.textContent = 'Saving…';
        gStatus.style.color = 'var(--text2)';
        fetch('/api/agent/hibernation/settings', {
          method: 'PATCH', headers: {'Content-Type':'application/json'},
          body: JSON.stringify(payload)
        }).then(function(r){ return r.json(); }).then(function(j) {
          if (j.ok) {
            gStatus.textContent = '✓ saved';
            gStatus.style.color = 'var(--green)';
          } else {
            gStatus.textContent = '✗ ' + (j.error || 'failed');
            gStatus.style.color = 'var(--red)';
          }
        }).catch(function() {
          gStatus.textContent = '✗ network error';
          gStatus.style.color = 'var(--red)';
        });
      });

      globalDetails.appendChild(gWrap);
      wrap.appendChild(globalDetails);

      agentInfoBody.appendChild(wrap);
    });
}

function saveTags(name, tags) {
  fetch('/api/agent/tags/' + encodeURIComponent(name), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tags: tags })
  }).then(function() { buildTagBar(); });
}

// ── Tag bar (sub-nav) ────────────────────────────────────────────────────────
var _activeTag = null;

var _searchQuery    = '';
// Snapshot of card positions taken when search transitions empty → non-empty,
// so we can restore the user's drag layout exactly when they clear the box.
var _searchSnapshot = null;

function buildTagBar() {
  var allTags = new Set();
  (window._agents || []).forEach(function(a) {
    (a.tags || []).forEach(function(t) { allTags.add(t); });
  });

  var bar    = document.getElementById('tag-bar');
  var search = document.getElementById('tag-bar-search');
  // remove old pills (keep label + search box)
  bar.querySelectorAll('.tag-pill').forEach(function(p) { p.remove(); });

  if (allTags.size) {
    // "All" pill — insert before search box
    var allPill = document.createElement('span');
    allPill.className = 'tag-pill all-pill' + (_activeTag === null ? ' active' : '');
    allPill.textContent = 'All';
    allPill.addEventListener('click', function() { setActiveTag(null); });
    bar.insertBefore(allPill, search);

    Array.from(allTags).sort().forEach(function(tag) {
      var pill = document.createElement('span');
      pill.className = 'tag-pill' + (_activeTag === tag ? ' active' : '');
      pill.textContent = tag;
      pill.addEventListener('click', function() { setActiveTag(tag); });
      bar.insertBefore(pill, search);
    });
  }
}

function setActiveTag(tag) {
  _activeTag = tag;
  buildTagBar();
  applyAllFilters();
}

function applyAllFilters() {
  var q         = _searchQuery.toLowerCase();
  var hadSearch = _searchSnapshot !== null;
  var hasSearch = q.length > 0;

  document.querySelectorAll('.agent-card').forEach(function(card) {
    var name      = card.dataset.agent || '';
    var agent     = (window._agents || []).find(function(a) { return a.name === name; });
    var agentTags = (agent && agent.tags) || [];
    var tagOk     = !_activeTag || agentTags.includes(_activeTag);
    var searchOk  = !q || name.toLowerCase().includes(q);
    var show      = tagOk && searchOk;
    card.style.opacity       = show ? '' : '0';
    card.style.pointerEvents = show ? '' : 'none';
    card.style.visibility    = show ? '' : 'hidden';
    card.dataset.tagHidden   = show ? '' : '1';
  });

  // Snapshot the user's persistent layout once, on the first keystroke,
  // so the search auto-pack below is reversible when they clear the box.
  if (!hadSearch && hasSearch) {
    _searchSnapshot = loadPos();
  }

  if (hasSearch) {
    // Flow visible matches into the top-left A-Z grid and scroll the
    // canvas back to (0,0) so it actually looks like the search worked.
    var visible = Object.values(agents)
      .filter(function(a) {
        return (!onlineOnly || a.data.online) && a.el.dataset.tagHidden !== '1';
      })
      .sort(function(a, b) { return a.data.name.localeCompare(b.data.name); });
    packGrid(visible);
    var canvasEl = document.getElementById('canvas');
    if (canvasEl) { canvasEl.scrollLeft = 0; canvasEl.scrollTop = 0; }
  } else if (hadSearch && _searchSnapshot) {
    // Search just cleared — put every card (and persisted positions) back
    // exactly where the user had them before they started typing.
    savePos(_searchSnapshot);
    Object.keys(_searchSnapshot).forEach(function(name) {
      var p    = _searchSnapshot[name];
      var card = document.querySelector('.agent-card[data-agent="' + CSS.escape(name) + '"]');
      if (!card || !p) return;
      card.style.left = p.x + 'px';
      card.style.top  = p.y + 'px';
    });
    _searchSnapshot = null;
    expandFloor();
  }
}

// kept for back-compat (called after fetchAgents)
function filterCardsByTag(tag) { applyAllFilters(); }

// ── agent search wiring ──
(function() {
  var inp   = document.getElementById('agent-search');
  var clear = document.getElementById('agent-search-clear');
  inp.addEventListener('input', function() {
    _searchQuery = inp.value;
    clear.classList.toggle('visible', !!inp.value);
    applyAllFilters();
  });
  clear.addEventListener('click', function() {
    inp.value = '';
    _searchQuery = '';
    clear.classList.remove('visible');
    applyAllFilters();
    inp.focus();
  });
  // keyboard shortcut: / or Ctrl+F focuses search
  document.addEventListener('keydown', function(e) {
    if ((e.key === '/' || (e.ctrlKey && e.key === 'f')) &&
        document.activeElement !== inp &&
        !e.target.closest('input, textarea, [contenteditable]')) {
      e.preventDefault();
      inp.focus();
      inp.select();
    }
    if (e.key === 'Escape' && document.activeElement === inp) {
      inp.value = '';
      _searchQuery = '';
      clear.classList.remove('visible');
      applyAllFilters();
      inp.blur();
    }
  });
})();

// ── Deploy Agent modal ──
var deployOverlay = document.getElementById('deploy-overlay');
var deployOutput  = document.getElementById('deploy-output');
var deployStatus  = document.getElementById('deploy-status');
var deployRun     = document.getElementById('deploy-run');
var _deployEs     = null;

document.getElementById('deploy-btn').addEventListener('click', function() {
  resetDeployModal();
  deployOverlay.classList.add('open');
  document.getElementById('d-name').focus();
});
document.getElementById('deploy-close').addEventListener('click', closeDeployModal);
deployOverlay.addEventListener('click', function(e) {
  if (e.target === deployOverlay) closeDeployModal();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && deployOverlay.classList.contains('open')) closeDeployModal();
});

deployRun.addEventListener('click', function() {
  var name = document.getElementById('d-name').value.trim();
  if (!name) {
    document.getElementById('d-name').focus();
    return;
  }
  deployRun.disabled = true;
  deployRun.textContent = 'Deploying…';
  deployOutput.style.display = 'block';
  deployOutput.textContent = '';
  deployStatus.style.display = 'none';
  deployStatus.className = '';

  var isMaster = document.getElementById('d-master').checked;
  var payload = {
    name:              name,
    interval:          parseInt(document.getElementById('d-interval').value) || 10,
    no_channel:        document.getElementById('d-no-channel').checked,
    no_webhook:        document.getElementById('d-no-webhook').checked,
    mailinbox_host:    document.getElementById('d-mb-host').value.trim(),
    mailinbox_email:   document.getElementById('d-mb-email').value.trim(),
    mailinbox_password: document.getElementById('d-mb-pass').value,
  };

  // Use SSE streaming for live output
  fetch('/api/deploy', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(function(resp) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = '';

    function pump() {
      return reader.read().then(function(result) {
        if (result.done) return;
        buf += decoder.decode(result.value, {stream: true});
        var lines = buf.split('\n');
        buf = lines.pop();
        lines.forEach(function(line) {
          if (!line.startsWith('data: ')) return;
          var raw = line.slice(6);
          try {
            var parsed = JSON.parse(raw);
            if (parsed && typeof parsed === 'object' && '__exit__' in parsed) {
              var code = parsed.__exit__;
              deployRun.disabled = false;
              deployRun.textContent = 'Deploy Again';
              deployStatus.style.display = 'inline';
              if (code === 0) {
                deployStatus.className = 'ok';
                deployStatus.textContent = '✓ Deployed successfully';
                if (isMaster) {
                  fetch('/api/agent/master/' + encodeURIComponent(name), {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({master: true})
                  }).catch(function(){});
                }
                fetchAgents();
              } else {
                deployStatus.className = 'err';
                deployStatus.textContent = '✗ Deploy failed (exit ' + code + ')';
              }
            } else {
              // Strip ANSI colour codes for clean output
              var text = String(parsed).replace(/\x1b\[[0-9;]*m/g, '');
              deployOutput.textContent += text + '\n';
              deployOutput.scrollTop = deployOutput.scrollHeight;
            }
          } catch(e) {}
        });
        return pump();
      });
    }
    return pump();
  }).catch(function(e) {
    deployRun.disabled = false;
    deployRun.textContent = 'Deploy';
    deployStatus.style.display = 'inline';
    deployStatus.className = 'err';
    deployStatus.textContent = '✗ ' + e;
  });
});

function resetDeployModal() {
  document.getElementById('d-name').value = '';
  document.getElementById('d-interval').value = '10';
  document.getElementById('d-master').checked = false;
  document.getElementById('d-no-channel').checked = false;
  document.getElementById('d-no-webhook').checked = false;
  document.getElementById('d-mb-host').value = '';
  document.getElementById('d-mb-email').value = '';
  document.getElementById('d-mb-pass').value = '';
  deployOutput.style.display = 'none';
  deployOutput.textContent = '';
  deployStatus.style.display = 'none';
  deployRun.disabled = false;
  deployRun.textContent = 'Deploy';
}

function closeDeployModal() {
  if (_deployEs) { _deployEs.close(); _deployEs = null; }
  deployOverlay.classList.remove('open');
}

// ── Migrate v2 -> v4 modal ────────────────────────────────────────────────
var migrateOverlay = document.getElementById('migrate-overlay');
var migrateOutput  = document.getElementById('migrate-output');
var migrateStatus  = document.getElementById('migrate-status');
var migrateRunBtn  = document.getElementById('migrate-run-btn');
var migratePrevBtn = document.getElementById('migrate-preview-btn');
var migrateSelect  = document.getElementById('m-source');
var migrateTarget  = document.getElementById('m-target');
var migrateSummary = document.getElementById('migrate-summary');
var migrateSrcMeta = document.getElementById('migrate-source-meta');
var migrateTgtMeta = document.getElementById('migrate-target-meta');
var _migrateAgents = [];   // cached list from /api/migrate/v2/list
var _migrateRunning = false;

document.getElementById('migrate-btn').addEventListener('click', function() {
  resetMigrateModal();
  migrateOverlay.classList.add('open');
  loadMigrateSourceList();
});
document.getElementById('migrate-close').addEventListener('click', closeMigrateModal);
migrateOverlay.addEventListener('click', function(e) {
  if (e.target === migrateOverlay) closeMigrateModal();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && migrateOverlay.classList.contains('open') && !_migrateRunning) {
    closeMigrateModal();
  }
});

function resetMigrateModal() {
  migrateSelect.innerHTML = '<option value="">Loading…</option>';
  migrateTarget.value = '';
  migrateSrcMeta.textContent = '';
  migrateTgtMeta.style.display = 'none';
  migrateTgtMeta.textContent = '';
  migrateSummary.innerHTML = '<div class="ms-empty">Pick a source agent to see the plan…</div>';
  migrateOutput.style.display = 'none';
  migrateOutput.textContent = '';
  migrateStatus.style.display = 'none';
  migrateRunBtn.disabled = false;
  migrateRunBtn.textContent = 'Migrate';
  migratePrevBtn.disabled = false;
  ['context','routines','utilities','docs','jobs'].forEach(function(k){
    document.getElementById('m-opt-' + k).checked = true;
  });
}

function closeMigrateModal() {
  if (_migrateRunning) return;
  migrateOverlay.classList.remove('open');
}

function loadMigrateSourceList() {
  fetch('/api/migrate/v2/list').then(function(r){return r.json();}).then(function(d){
    _migrateAgents = d.agents || [];
    if (!_migrateAgents.length) {
      migrateSelect.innerHTML = '<option value="">No v2 agents found' + (d.v2_root ? ' at ' + d.v2_root : ' (set JARVIS_V2_ROOT)') + '</option>';
      return;
    }
    migrateSelect.innerHTML = '<option value="">— select a v2 agent —</option>'
      + _migrateAgents.map(function(a){
          var lbl = a.name + (a.channel ? '  (#' + a.channel + ')' : '')
                  + (a.v4_exists ? '  [v4 EXISTS]' : '');
          return '<option value="' + a.name + '"' + (a.v4_exists ? ' disabled' : '') + '>' + lbl + '</option>';
        }).join('');
  }).catch(function(){
    migrateSelect.innerHTML = '<option value="">Failed to load v2 agents</option>';
  });
}

migrateSelect.addEventListener('change', function() {
  var name = migrateSelect.value;
  if (!name) {
    migrateTarget.value = '';
    migrateSrcMeta.textContent = '';
    migrateSummary.innerHTML = '<div class="ms-empty">Pick a source agent to see the plan…</div>';
    return;
  }
  var info = _migrateAgents.find(function(a){return a.name === name;}) || {};
  migrateTarget.value = info.v4_suggested || name;
  var bits = [];
  if (info.channel)  bits.push('channel #' + info.channel);
  if (info.ssh_host) bits.push('ssh ' + info.ssh_host);
  if (info.ctx_size) bits.push((info.ctx_size/1024).toFixed(1) + ' KB context');
  migrateSrcMeta.textContent = bits.join('  ·  ');
  refreshMigratePreview();
});

migrateTarget.addEventListener('input', function() {
  refreshMigratePreview();
});

['context','routines','utilities','docs','jobs','archive'].forEach(function(k){
  document.getElementById('m-opt-' + k).addEventListener('change', refreshMigratePreview);
});

function migrateOptions() {
  return {
    context:   document.getElementById('m-opt-context').checked,
    routines:  document.getElementById('m-opt-routines').checked,
    utilities: document.getElementById('m-opt-utilities').checked,
    docs:      document.getElementById('m-opt-docs').checked,
    jobs_done: document.getElementById('m-opt-jobs').checked,
    archive:   document.getElementById('m-opt-archive').checked,
  };
}

var _migratePreviewT = null;
function refreshMigratePreview() {
  if (_migratePreviewT) clearTimeout(_migratePreviewT);
  _migratePreviewT = setTimeout(buildMigratePreview, 250);
}

function buildMigratePreview() {
  var v2 = migrateSelect.value;
  var v4 = migrateTarget.value.trim();
  if (!v2) return;
  fetch('/api/migrate/v2/preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({v2_name: v2, v4_name: v4, options: migrateOptions()})
  }).then(function(r){return r.json();}).then(renderMigrateSummary)
    .catch(function(){
      migrateSummary.innerHTML = '<div class="ms-err">Failed to build preview.</div>';
    });
}

migratePrevBtn.addEventListener('click', buildMigratePreview);

function renderMigrateSummary(plan) {
  var d   = plan.derived || {};
  var s   = plan.copy_summary || {};
  var rows = [];

  if (plan.errors && plan.errors.length) {
    plan.errors.forEach(function(e){
      rows.push('<div class="ms-row"><span class="ms-key ms-err">Error</span><span class="ms-val ms-err">' + escapeHtmlSafe(e) + '</span></div>');
    });
  }
  if (plan.warnings && plan.warnings.length) {
    plan.warnings.forEach(function(w){
      rows.push('<div class="ms-row"><span class="ms-key ms-warn">Warn</span><span class="ms-val ms-warn">' + escapeHtmlSafe(w) + '</span></div>');
    });
  }
  rows.push('<div class="ms-row"><span class="ms-key">Channel</span><span class="ms-val">#' + escapeHtmlSafe(d.channel || '?') + '</span></div>');
  rows.push('<div class="ms-row"><span class="ms-key">SSH host</span><span class="ms-val">' + escapeHtmlSafe(d.ssh_host || '?') + '</span></div>');
  rows.push('<div class="ms-row"><span class="ms-key">Web root</span><span class="ms-val">' + escapeHtmlSafe(d.web_root || '(unknown)') + '</span></div>');
  if (d.webhook_url) {
    var wh = d.webhook_url;
    var tail = wh.length > 20 ? '…' + wh.slice(-12) : wh;
    rows.push('<div class="ms-row"><span class="ms-key">Webhook</span><span class="ms-val">…' + escapeHtmlSafe(tail) + '</span></div>');
  } else {
    rows.push('<div class="ms-row"><span class="ms-key">Webhook</span><span class="ms-val ms-warn">none (replies will not post)</span></div>');
  }
  rows.push('<div class="ms-row"><span class="ms-key">Interval</span><span class="ms-val">' + (d.interval || 10) + 's</span></div>');
  rows.push('<div class="ms-row"><span class="ms-key">tmux</span><span class="ms-val">' + escapeHtmlSafe(plan.session || '') + '</span></div>');
  var copyBits = [
    'context.md ' + (s.context_md_bytes || 0) + 'B',
    'routines ' + s.routines_files,
    'utilities ' + s.utilities_files,
    'docs ' + s.docs_files,
    'jobs/done ' + s.jobs_done_files,
  ].join('  ·  ');
  rows.push('<div class="ms-row"><span class="ms-key">Copy</span><span class="ms-val">' + copyBits + '  =  ' + Math.round((s.total_bytes||0)/1024) + ' KB total</span></div>');

  if (s.archive_dest) {
    var aCls = s.archive_collision ? 'ms-val ms-warn' : 'ms-val';
    rows.push('<div class="ms-row"><span class="ms-key">Archive</span><span class="' + aCls + '">' + escapeHtmlSafe(s.archive_dest) + '</span></div>');
  } else {
    rows.push('<div class="ms-row"><span class="ms-key">Archive</span><span class="ms-val ms-warn">off (v2 dir left in place)</span></div>');
  }

  migrateSummary.innerHTML = rows.join('');

  // Target conflict feedback (UI-only, server still does the real check)
  if (plan.errors && plan.errors.some(function(e){return e.indexOf('already exists') >= 0;})) {
    migrateTgtMeta.style.display = 'block';
    migrateTgtMeta.textContent = '✗ v4 agent already exists with this name';
    migrateRunBtn.disabled = true;
  } else {
    migrateTgtMeta.style.display = 'none';
    migrateRunBtn.disabled = !plan.ok;
  }
}

function escapeHtmlSafe(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

migrateRunBtn.addEventListener('click', function() {
  var v2 = migrateSelect.value;
  var v4 = migrateTarget.value.trim();
  if (!v2 || !v4) return;
  if (!confirm('Migrate v2:' + v2 + ' to v4:' + v4 + '?\\n\\nThis scaffolds a new v4 agent. tmux will NOT be started — you can launch it from the agent card afterwards.')) return;

  _migrateRunning = true;
  migrateRunBtn.disabled = true;
  migratePrevBtn.disabled = true;
  migrateRunBtn.textContent = 'Migrating…';
  migrateOutput.style.display = 'block';
  migrateOutput.textContent = '';
  migrateStatus.style.display = 'none';
  migrateStatus.className = '';

  fetch('/api/migrate/v2/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({v2_name: v2, v4_name: v4, options: migrateOptions()})
  }).then(function(resp) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buf = '';
    function pump() {
      return reader.read().then(function(result) {
        if (result.done) return;
        buf += decoder.decode(result.value, {stream: true});
        var lines = buf.split('\n');
        buf = lines.pop();
        lines.forEach(function(line) {
          if (!line.startsWith('data: ')) return;
          var raw = line.slice(6);
          try {
            var parsed = JSON.parse(raw);
            if (parsed && typeof parsed === 'object' && '__exit__' in parsed) {
              _migrateRunning = false;
              migrateRunBtn.disabled = false;
              migratePrevBtn.disabled = false;
              migrateRunBtn.textContent = 'Migrate Again';
              migrateStatus.style.display = 'inline';
              if (parsed.__exit__ === 0) {
                migrateStatus.className = 'ok';
                migrateStatus.textContent = '✓ Scaffolded — review then click Restart on the card';
                fetchAgents();
                if (typeof toast === 'function') {
                  toast('Migrated ' + (parsed.v4_name || v4) + ' — review apps/rocketchat.py + context.md');
                }
              } else {
                migrateStatus.className = 'err';
                migrateStatus.textContent = '✗ Migration failed (exit ' + parsed.__exit__ + ')';
              }
            } else {
              migrateOutput.textContent += String(parsed) + '\n';
              migrateOutput.scrollTop = migrateOutput.scrollHeight;
            }
          } catch(e) { /* ignore parse errors */ }
        });
        return pump();
      });
    }
    return pump();
  }).catch(function(e) {
    _migrateRunning = false;
    migrateRunBtn.disabled = false;
    migratePrevBtn.disabled = false;
    migrateRunBtn.textContent = 'Migrate';
    migrateStatus.style.display = 'inline';
    migrateStatus.className = 'err';
    migrateStatus.textContent = '✗ ' + e;
  });
});

// ── file browser (docs / apps / modules) ──
var browserPanel   = document.getElementById('browser-panel');
var browserTree    = document.getElementById('browser-tree');
var browserBody    = document.getElementById('browser-body');
var browserPath    = document.getElementById('browser-file-path');
var _browserSection = null;
var _browserActive  = null;

document.getElementById('browser-close').addEventListener('click', closeBrowser);

document.getElementById('map-link').addEventListener('click', function() {
  closeBrowser();
});

document.querySelectorAll('.hdr-nav-link[data-section]').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var section = this.dataset.section;
    if (browserPanel.classList.contains('open') && _browserSection === section) {
      closeBrowser();
    } else {
      openBrowser(section);
    }
  });
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && browserPanel.classList.contains('open')) closeBrowser();
});

function openBrowser(section) {
  _browserSection = section;
  _browserActive  = null;
  browserPanel.classList.add('open');
  browserBody.innerHTML = '<div style="color:var(--text2);font-size:13px;padding:40px;text-align:center;opacity:0.4;">Select a file from the tree</div>';
  browserPath.textContent = section + '/';

  // Auto-load README.md for modules section
  if (section === 'modules') {
    loadFile('modules', 'README.md');
  }

  // Highlight active nav link
  document.querySelectorAll('.hdr-nav-link').forEach(function(b) {
    b.classList.toggle('active', b.dataset.section === section);
  });
  document.getElementById('map-link').classList.remove('active');

  // Load file tree.
  // For section==='agents' we also fetch /api/agents in parallel so we can
  // render a per-agent Start (offline) or Stop (online) button next to Archive.
  browserTree.innerHTML = '<div class="tree-section">' + section.toUpperCase() + '</div>';
  var fetches = [fetch('/api/browser/list?section=' + section).then(function(r){ return r.json(); })];
  if (section === 'agents') {
    fetches.push(fetch('/api/agents').then(function(r){ return r.json(); }));
  }
  Promise.all(fetches)
    .then(function(results) {
      var d = results[0];
      var agentOnline = {};
      if (section === 'agents' && Array.isArray(results[1])) {
        results[1].forEach(function(a) { agentOnline[a.name] = !!a.online; });
      }
      browserTree.innerHTML = '<div class="tree-section">' + section.toUpperCase() + '</div>';
      if (!d.files || !d.files.length) {
        browserTree.innerHTML += '<div style="padding:12px;font-size:11px;color:var(--text2);opacity:0.4;">No files found</div>';
        return;
      }
      // Group files by top-level directory
      var groups = {};   // dir → [file, ...]
      var rootFiles = [];
      d.files.forEach(function(f) {
        var parts = f.path.split('/');
        if (parts.length === 1) {
          rootFiles.push(f);
        } else {
          var dir = parts[0];
          if (!groups[dir]) groups[dir] = [];
          groups[dir].push(f);
        }
      });

      function makeFileItem(f, sec) {
        var parts = f.path.split('/');
        var fname = parts[parts.length - 1];
        var ext   = fname.split('.').pop().toLowerCase();
        var icon  = ext === 'md' ? '📄' : ext === 'py' ? '🐍' : ext === 'php' ? '🐘' : '📎';
        var item  = document.createElement('div');
        item.className = 'tree-item';
        item.innerHTML = '<span class="tree-item-icon">' + icon + '</span><span>' + fname + '</span>';
        item.title = f.path;
        item.addEventListener('click', function() {
          document.querySelectorAll('.tree-item').forEach(function(i) { i.classList.remove('active'); });
          item.classList.add('active');
          loadFile(sec, f.path);
        });
        return item;
      }

      // Root-level files first
      var autoLoadPath = null;
      rootFiles.forEach(function(f) {
        var item = makeFileItem(f, section);
        if (section === 'modules' && f.path === 'README.md') {
          item.classList.add('active');
          autoLoadPath = f.path;
        }
        browserTree.appendChild(item);
      });

      // Folders as collapsible dropdowns
      Object.keys(groups).sort().forEach(function(dir) {
        var folder = document.createElement('div');
        folder.className = 'tree-folder';

        var folderInner = '<span class="tree-folder-arrow">▶</span><span>📁</span><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;">' + dir + '</span>';

        // Lifecycle buttons for active agents; Restore for archived.
        // Order: Start/Stop (state-dependent) first, then Archive — matches
        // the natural left-to-right "what would I do next" mental model.
        if (section === 'agents') {
          if (agentOnline[dir]) {
            folderInner += '<button class="tree-action-btn tree-stop-btn" title="Stop agent (kill tmux session)" data-agent="' + dir + '">stop</button>';
          } else {
            folderInner += '<button class="tree-action-btn tree-start-btn" title="Start agent (relaunch tmux + cursor + monitor)" data-agent="' + dir + '">start</button>';
          }
          folderInner += '<button class="tree-action-btn tree-archive-btn" title="Archive agent" data-agent="' + dir + '">archive</button>';
        } else if (section === 'archive') {
          folderInner += '<button class="tree-action-btn tree-restore-btn" title="Restore agent" data-agent="' + dir + '">restore</button>';
        }
        folder.innerHTML = folderInner;
        folder.style.display = 'flex';
        folder.style.alignItems = 'center';
        folder.style.gap = '4px';

        var children = document.createElement('div');
        children.className = 'tree-folder-children';

        // For agents/archive section, group sub-dirs inside each agent
        if (section === 'agents' || section === 'archive') {
          // sub-group by subdir (docs/, utilities/, routines/) within this agent
          var subGroups = {};
          var agentRoot = [];
          groups[dir].forEach(function(f) {
            var parts = f.path.split('/');
            // parts[0] = agent name, parts[1] = subdir or filename
            if (parts.length === 2) {
              agentRoot.push(f);  // e.g. context.md
            } else {
              var sub = parts[1];
              if (!subGroups[sub]) subGroups[sub] = [];
              subGroups[sub].push(f);
            }
          });
          // root files first (context.md)
          agentRoot.forEach(function(f) {
            children.appendChild(makeFileItem(f, section));
          });
          // sub-dirs
          Object.keys(subGroups).sort().forEach(function(sub) {
            var subFolder = document.createElement('div');
            subFolder.className = 'tree-folder';
            subFolder.style.paddingLeft = '8px';
            subFolder.innerHTML = '<span class="tree-folder-arrow">▶</span><span>📁</span><span>' + sub + '</span>';
            var subChildren = document.createElement('div');
            subChildren.className = 'tree-folder-children';
            subGroups[sub].forEach(function(f) {
              subChildren.appendChild(makeFileItem(f, section));
            });
            subFolder.addEventListener('click', function(e) {
              e.stopPropagation();
              var isOpen = subFolder.classList.contains('open');
              subFolder.classList.toggle('open', !isOpen);
              subChildren.classList.toggle('open', !isOpen);
            });
            children.appendChild(subFolder);
            children.appendChild(subChildren);
          });
        } else {
          groups[dir].forEach(function(f) {
            children.appendChild(makeFileItem(f, section));
          });
        }

        // For agents: clicking the folder header auto-loads context.md
        folder.addEventListener('click', function() {
          var isOpen = folder.classList.contains('open');
          folder.classList.toggle('open', !isOpen);
          children.classList.toggle('open', !isOpen);
          if ((section === 'agents' || section === 'archive') && !isOpen) {
            // auto-load context.md on first open
            var ctxPath = dir + '/context.md';
            var ctxItem = children.querySelector('.tree-item');
            if (ctxItem && ctxItem.title === ctxPath) {
              document.querySelectorAll('.tree-item').forEach(function(i) { i.classList.remove('active'); });
              ctxItem.classList.add('active');
              loadFile(section, ctxPath);
            }
          }
        });

        browserTree.appendChild(folder);
        browserTree.appendChild(children);

      });

      // Wire start / stop / archive / restore buttons (added after DOM insertion).
      // Start is no-confirm (safe relaunch). Stop and Archive are confirm-gated
      // because they interrupt running work / move files.
      browserTree.querySelectorAll('.tree-start-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          var agent = this.dataset.agent;
          var self  = this;
          self.textContent = '…';
          self.disabled = true;
          toast('⏳ Starting ' + agent + '…');
          fetch('/api/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: agent})
          }).then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok) {
              toast('✓ Started ' + agent);
              fetchAgents();
              openBrowser('agents');   // refresh tree (button flips to Stop)
            } else {
              var why = (d.stderr && d.stderr.trim().split('\n').pop()) || d.error || 'Start failed';
              toast('✗ ' + why, 4500);
              self.textContent = 'start';
              self.disabled = false;
            }
          }).catch(function() {
            toast('✗ Start request failed', 3500);
            self.textContent = 'start';
            self.disabled = false;
          });
        });
      });

      browserTree.querySelectorAll('.tree-stop-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          var agent = this.dataset.agent;
          if (!confirm('Stop agent "' + agent + '"?\n\nThis kills its tmux session (pane 1 cursor agent + pane 2 RC monitor). The agent stops responding to RocketChat messages until restarted. Files are preserved.')) return;
          var self  = this;
          self.textContent = '…';
          self.disabled = true;
          fetch('/api/stop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: agent})
          }).then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok) {
              toast('✓ Stopped ' + agent);
              fetchAgents();
              openBrowser('agents');   // refresh tree (button flips to Start)
            } else {
              toast('✗ ' + (d.error || 'Stop failed'), 3500);
              self.textContent = 'stop';
              self.disabled = false;
            }
          }).catch(function() {
            toast('✗ Stop request failed', 3500);
            self.textContent = 'stop';
            self.disabled = false;
          });
        });
      });

      browserTree.querySelectorAll('.tree-archive-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          var agent = this.dataset.agent;
          if (!confirm('Archive agent "' + agent + '"?\n\nThis will kill its tmux session and move its files to archive/. You can restore it later from the Archive tab.')) return;
          fetch('/api/agent/archive', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: agent})
          }).then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok) {
              toast(d.message);
              fetchAgents();
              openBrowser('agents');   // refresh tree
            } else {
              toast(d.error || 'Archive failed');
            }
          });
        });
      });

      browserTree.querySelectorAll('.tree-restore-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
          e.stopPropagation();
          var agent = this.dataset.agent;
          if (!confirm('Restore "' + agent + '" back to agents/?')) return;
          fetch('/api/agent/restore', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: agent})
          }).then(function(r) { return r.json(); }).then(function(d) {
            if (d.ok) {
              toast(d.message);
              fetchAgents();
              openBrowser('archive');  // refresh tree
            } else {
              toast(d.error || 'Restore failed');
            }
          });
        });
      });

      // Auto-load for modules (README.md) and agents (first agent's context.md)
      if (autoLoadPath) {
        loadFile(section, autoLoadPath);
      } else if (section === 'agents' || section === 'archive') {
        // Auto-open first folder and load its context.md
        var firstFolder = browserTree.querySelector('.tree-folder');
        var firstChildren = firstFolder ? firstFolder.nextSibling : null;
        if (firstFolder && firstChildren) {
          firstFolder.classList.add('open');
          firstChildren.classList.add('open');
          var firstCtx = firstChildren.querySelector('.tree-item');
          if (firstCtx) {
            firstCtx.classList.add('active');
            loadFile(section, firstCtx.title);
          }
        }
      }
    });
}

function loadFile(section, path) {
  _browserActive = path;
  browserPath.textContent = section + '/' + path;
  browserBody.innerHTML = '<div style="color:var(--text2);font-size:12px;padding:40px;text-align:center;opacity:0.4;">Loading…</div>';
  fetch('/api/browser/file?section=' + section + '&path=' + encodeURIComponent(path))
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.error) {
        browserBody.innerHTML = '<div style="color:var(--red);padding:24px;">' + escHtml(d.error) + '</div>';
        return;
      }
      if (d.kind === 'markdown' && window.marked) {
        browserBody.innerHTML = marked.parse(d.content);
      } else {
        var pre = document.createElement('pre');
        pre.className = 'code-plain';
        var code = document.createElement('code');
        code.textContent = d.content;
        pre.appendChild(code);
        browserBody.innerHTML = '';
        browserBody.appendChild(pre);
      }
      browserBody.scrollTop = 0;
    });
}

function closeBrowser() {
  browserPanel.classList.remove('open');
  document.querySelectorAll('.hdr-nav-link').forEach(function(b) { b.classList.remove('active'); });
  document.getElementById('map-link').classList.add('active');
  _browserSection = null;
}

// Map link is active by default
document.getElementById('map-link').classList.add('active');

})();
</script>

<!-- ── Floating Rocket.Chat feed (global message viewer) ───────────────── -->
<div id="rc-feed" aria-label="Rocket.Chat live feed">
  <div id="rc-feed-bar" id-tooltip="Drag to reposition">
    <div id="rc-feed-title">
      <div id="rc-feed-dot" class="loading"></div>
      <span>Rocket.Chat</span>
      <span id="rc-feed-count" style="color:var(--text2);font-weight:500;letter-spacing:0;text-transform:none;font-size:10px;"></span>
    </div>
    <div id="rc-feed-actions">
      <button id="rc-feed-refresh" title="Refresh now">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
      </button>
      <button id="rc-feed-hide" title="Hide" style="font-size:18px;font-weight:600;line-height:0.7;">−</button>
    </div>
  </div>
  <div id="rc-feed-body">
    <div style="padding:24px 12px; font-size:11px; color:var(--text2); text-align:center;">Loading messages…</div>
  </div>
  <div id="rc-feed-status">—</div>
</div>
<button type="button" id="rc-feed-toggle" title="Show Rocket.Chat feed">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
</button>

<!-- ── Quick Task dialog ──────────────────────────────────────────────── -->
<button type="button" id="task-dialog-toggle" title="Quick Task — delegate to an agent">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M9 9l2 2 4-4"/><path d="M9 14h6"/><path d="M9 18h4"/></svg>
</button>
<div id="task-dialog" class="hidden" aria-label="Quick task dialog">
  <div id="task-dialog-bar">
    <div id="task-dialog-title">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M9 9l2 2 4-4"/></svg>
      <span>Quick Task</span>
    </div>
    <div id="task-dialog-actions">
      <button id="task-dialog-close" title="Close (Esc)" style="line-height:0.7;">−</button>
    </div>
  </div>
  <div id="task-dialog-body">
    <label for="task-agent">Delegate to</label>
    <select id="task-agent"><option value="">Loading agents…</option></select>
    <label for="task-text">Task</label>
    <textarea id="task-text" rows="4" placeholder="What should the agent do? e.g. &quot;Check disk usage and send a one-line summary.&quot;"></textarea>
  </div>
  <div id="task-dialog-footer">
    <button id="task-delegate-btn" type="button">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      Delegate
    </button>
    <span id="task-dialog-status">—</span>
    <span id="task-dialog-hint" title="Cmd/Ctrl+Enter to send">⌘↵</span>
  </div>
</div>

<!-- ── Task Planner ───────────────────────────────────────────────────── -->
<button type="button" id="planner-toggle" title="Task Planner — your work & projects">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M9 11l3 3L22 4"/>
    <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
  </svg>
  <span class="badge zero" id="planner-badge">0</span>
</button>
<div id="planner-panel" class="hidden" aria-label="Task planner">
  <div id="planner-bar">
    <div id="planner-title">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>
      <span>Task Planner</span>
      <span class="count" id="planner-count">0 active</span>
    </div>
    <div id="planner-actions">
      <button id="planner-staff-btn" title="Manage staff & daily digest"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></button>
      <button id="planner-newproj-btn" title="New project">+</button>
      <button id="planner-close-btn" title="Close (Esc)" style="line-height:0.7;">−</button>
    </div>
  </div>
  <div id="planner-staff-drawer" aria-label="Staff and digest settings">
    <div class="staff-section-title">Staff (daily digest recipients)</div>
    <div class="staff-list" id="planner-staff-list">
      <div class="staff-row" style="color:var(--text2);font-size:11px;">Loading…</div>
    </div>
    <div class="staff-add-row">
      <input id="planner-staff-add-input" type="text" placeholder="Add Rocket.Chat user… (type to search)" autocomplete="off"/>
      <div class="staff-suggest" id="planner-staff-suggest"></div>
    </div>
    <div id="planner-digest-strip">
      <label>Digest</label>
      <input type="time" id="planner-digest-time" value="08:00"/>
      <label class="toggle"><input type="checkbox" id="planner-digest-enabled"/> enabled</label>
      <span class="last-sent" id="planner-digest-last">—</span>
    </div>
  </div>
  <div id="planner-add-row">
    <input id="planner-add-input" type="text"
           placeholder="Add task…  try !high, due:fri, #project, @alice" autocomplete="off"/>
    <button id="planner-add-btn" type="button" title="Add (Enter)">+</button>
  </div>
  <div id="planner-controls">
    <select id="planner-project-filter" title="Show only this project">
      <option value="">All projects</option>
    </select>
    <select id="planner-assignee-filter" title="Show only this assignee">
      <option value="">All assignees</option>
    </select>
    <span class="planner-chip active" data-filter="all">All</span>
    <span class="planner-chip" data-filter="today">Today</span>
    <span class="planner-chip" data-filter="active">Active</span>
    <span class="planner-chip" data-filter="done">Done</span>
  </div>
  <div id="planner-list"></div>
  <div id="planner-footer">
    <span id="planner-status">—</span>
    <span id="planner-next-digest" title="Next daily digest fire" style="margin-left:auto;margin-right:8px;"></span>
    <button id="planner-cleanup-btn" title="Remove all completed tasks">Clear done</button>
  </div>
</div>

<!-- ── Calendar ───────────────────────────────────────────────────────── -->
<button type="button" id="calendar-toggle" title="Calendar — monthly schedule">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
    <line x1="16" y1="2" x2="16" y2="6"/>
    <line x1="8" y1="2" x2="8" y2="6"/>
    <line x1="3" y1="10" x2="21" y2="10"/>
  </svg>
  <span class="badge zero" id="calendar-badge">0</span>
</button>
<div id="calendar-panel" class="hidden" aria-label="Calendar">
  <div id="calendar-bar">
    <div id="calendar-title">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
      <span>Calendar</span>
    </div>
    <div id="calendar-actions">
      <button id="calendar-close-btn" title="Close (Esc)" style="line-height:0.7;">−</button>
    </div>
  </div>
  <div id="calendar-nav">
    <button id="calendar-prev"  title="Previous month">&lsaquo;</button>
    <span id="calendar-month-label">—</span>
    <button id="calendar-next"  title="Next month">&rsaquo;</button>
    <button id="calendar-today-btn" title="Jump to today">TODAY</button>
  </div>
  <div id="calendar-grid"></div>
  <div id="calendar-day-detail">
    <div class="cal-detail-empty">Pick a day above to see what's scheduled.</div>
  </div>
  <div id="calendar-status">—</div>
</div>

<script>
/* ── Floating Rocket.Chat feed ───────────────────────────────────────── */
(function() {
  var KEY_HIDDEN   = 'jv_rc_hidden';
  var KEY_POS      = 'jv_rc_pos';
  var KEY_LASTSEEN = 'jv_rc_lastseen';
  var REFRESH_MS   = 30000;

  var panel       = document.getElementById('rc-feed');
  var bar         = document.getElementById('rc-feed-bar');
  var body        = document.getElementById('rc-feed-body');
  var statusEl    = document.getElementById('rc-feed-status');
  var dot         = document.getElementById('rc-feed-dot');
  var countEl     = document.getElementById('rc-feed-count');
  var btnRefresh  = document.getElementById('rc-feed-refresh');
  var btnHide     = document.getElementById('rc-feed-hide');
  var btnShow     = document.getElementById('rc-feed-toggle');

  var loading  = false;
  var lastData = null;
  var lastSeen = '';
  try { lastSeen = localStorage.getItem(KEY_LASTSEEN) || ''; } catch(e){}

  function setHidden(v) {
    try { localStorage.setItem(KEY_HIDDEN, v ? '1' : '0'); } catch(e){}
    if (v) { panel.classList.add('hidden'); btnShow.classList.add('visible'); }
    else   { panel.classList.remove('hidden'); btnShow.classList.remove('visible'); }
  }
  try { setHidden(localStorage.getItem(KEY_HIDDEN) === '1'); } catch(e) {}

  /* restore saved position (drag persistence) */
  try {
    var savedPos = JSON.parse(localStorage.getItem(KEY_POS) || 'null');
    if (savedPos && typeof savedPos.right === 'number' && typeof savedPos.bottom === 'number') {
      panel.style.right  = savedPos.right + 'px';
      panel.style.bottom = savedPos.bottom + 'px';
    }
  } catch(e){}

  /* drag-to-reposition + persist on mouseup */
  (function() {
    var dragging = false, ox, oy, sr, sb;
    bar.addEventListener('mousedown', function(e) {
      if (e.target.closest('button')) return;
      dragging = true; ox = e.clientX; oy = e.clientY;
      var r = panel.getBoundingClientRect();
      sr = window.innerWidth  - r.right;
      sb = window.innerHeight - r.bottom;
      e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
      if (!dragging) return;
      var nr = Math.max(0, sr - (e.clientX - ox));
      var nb = Math.max(0, sb - (e.clientY - oy));
      panel.style.right  = nr + 'px';
      panel.style.bottom = nb + 'px';
    });
    document.addEventListener('mouseup', function() {
      if (!dragging) return;
      dragging = false;
      try {
        localStorage.setItem(KEY_POS, JSON.stringify({
          right:  parseInt(panel.style.right, 10)  || 20,
          bottom: parseInt(panel.style.bottom, 10) || 20,
        }));
      } catch(e){}
    });
  })();

  function relTime(ts) {
    var d = new Date(ts), diff = Math.round((Date.now() - d) / 1000);
    if (diff < 5)     return 'just now';
    if (diff < 60)    return diff + 's ago';
    if (diff < 3600)  return Math.floor(diff / 60)  + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
  }
  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function setDot(state) {
    dot.classList.remove('loading','ok','error');
    if (state) dot.classList.add(state);
  }
  function roomIcon(t) {
    /* lock = private group, # = channel, @ = DM */
    if (t === 'p') return '<span class="rc-row-room-icon" title="Private group">🔒</span>';
    if (t === 'c') return '<span class="rc-row-room-icon" title="Channel">#</span>';
    if (t === 'd') return '<span class="rc-row-room-icon" title="Direct message">@</span>';
    return '';
  }

  function applyUnread() {
    if (!lastData || !(lastData.messages || []).length) {
      panel.classList.remove('has-unread');
      btnShow.classList.remove('has-unread');
      return;
    }
    var newest = lastData.messages[0].ts || '';
    var has    = lastSeen && newest > lastSeen;
    /* never highlight the panel for messages we sent ourselves */
    if (has) {
      var newestUser = lastData.messages[0].username || '';
      if (newestUser === (lastData.me_username || '')) has = false;
    }
    panel.classList.toggle('has-unread', !!has);
    btnShow.classList.toggle('has-unread', !!has);
  }
  function markSeen() {
    if (!lastData || !(lastData.messages || []).length) return;
    lastSeen = lastData.messages[0].ts || '';
    try { localStorage.setItem(KEY_LASTSEEN, lastSeen); } catch(e){}
    applyUnread();
  }

  function render(data) {
    var me   = data.me_username || '';
    var msgs = (data.messages || []).slice(0, 25);
    countEl.textContent = msgs.length ? '· ' + msgs.length : '';
    if (!msgs.length) {
      body.innerHTML = '<div style="padding:18px 12px; font-size:11px; color:var(--text2); text-align:center;">No recent messages</div>';
      return;
    }
    body.innerHTML = msgs.map(function(m) {
      var mine   = (m.username === me);
      var room   = m.room_name || '?';
      var orphan = !m.agent;
      return '<div class="rc-row' + (mine ? ' rc-mine' : '') + (orphan ? ' rc-orphan' : '') + '"'
        +    ' data-agent="'  + esc(m.agent || '')        + '"'
        +    ' data-room="'   + esc(room)                  + '"'
        +    ' data-type="'   + esc(m.room_type || '')     + '">'
        +   '<div class="rc-row-room">'
        +     roomIcon(m.room_type)
        +     '<span>' + esc(room) + '</span>'
        +   '</div>'
        +   '<div class="rc-row-meta">'
        +     '<span class="rc-row-user">' + esc(m.username || '?') + (mine ? ' (you)' : '') + '</span>'
        +     '<span class="rc-row-time">' + relTime(m.ts) + '</span>'
        +   '</div>'
        +   '<div class="rc-row-text">' + esc(m.msg || '(attachment)') + '</div>'
        + '</div>';
    }).join('');
  }

  /* click message → if it's a v4 agent room, open the agent settings popup;
     otherwise open the Rocket.Chat web client directly to that room */
  body.addEventListener('click', function(e) {
    var row = e.target.closest('.rc-row');
    if (!row) return;
    var agent = row.dataset.agent;
    if (agent && typeof window.openAgentInfo === 'function') {
      window.openAgentInfo(agent);
      return;
    }
    var room = row.dataset.room;
    var t    = row.dataset.type;
    if (room) {
      var prefix = t === 'p' ? 'group' : (t === 'd' ? 'direct' : 'channel');
      var rcBase = {{ rocketchat_url | tojson }};
      if (rcBase) {
        window.open(rcBase + '/' + prefix + '/' + encodeURIComponent(room), '_blank', 'noopener');
      }
    }
  });

  function load() {
    if (loading) return;
    loading = true;
    setDot('loading');
    statusEl.textContent = 'Fetching…';
    fetch('/api/rocketchat/feed', { cache: 'no-store' })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        loading = false;
        if (!data.ok) {
          setDot('error');
          statusEl.textContent = (data.error || 'Error').slice(0, 80);
          body.innerHTML = '<div style="padding:18px 12px; font-size:11px; color:var(--red); text-align:center;">' + esc(data.error || 'Error') + '</div>';
          return;
        }
        setDot('ok');
        lastData = data;
        render(data);
        applyUnread();
        var n  = (data.messages || []).length;
        var t  = data.fetched_at ? new Date(data.fetched_at).toLocaleTimeString() : '';
        var rs = data.rooms_seen || 0;
        statusEl.textContent = n + ' msg' + (n === 1 ? '' : 's') + ' · ' + rs + ' rooms · ' + t;
      })
      .catch(function() {
        loading = false;
        setDot('error');
        statusEl.textContent = 'Network error';
        body.innerHTML = '<div style="padding:18px 12px; font-size:11px; color:var(--red); text-align:center;">Fetch failed</div>';
      });
  }

  btnRefresh.addEventListener('click', load);
  btnHide.addEventListener('click', function() { setHidden(true); });
  btnShow.addEventListener('click', function() {
    setHidden(false);
    if (lastData) markSeen();
    load();
  });

  /* refresh on tab refocus so we never show stale data */
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden && !panel.classList.contains('hidden')) load();
  });

  /* clear unread when user hovers the open panel for ~1.5s */
  var hoverTimer = null;
  panel.addEventListener('mouseenter', function() {
    if (panel.classList.contains('hidden')) return;
    hoverTimer = setTimeout(markSeen, 1500);
  });
  panel.addEventListener('mouseleave', function() {
    if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
  });

  /* boot */
  load();
  setInterval(load, REFRESH_MS);
})();

/* ── Quick Task dialog ───────────────────────────────────────────────── */
(function() {
  var dialog  = document.getElementById('task-dialog');
  var toggle  = document.getElementById('task-dialog-toggle');
  var bar     = document.getElementById('task-dialog-bar');
  var closeBtn = document.getElementById('task-dialog-close');
  var sel     = document.getElementById('task-agent');
  var ta      = document.getElementById('task-text');
  var btn     = document.getElementById('task-delegate-btn');
  var status  = document.getElementById('task-dialog-status');
  if (!dialog || !toggle) return;

  var LS_HIDDEN = 'jv_task_hidden';
  var LS_POS    = 'jv_task_pos';
  var LS_AGENT  = 'jv_task_last_agent';

  /* visibility persistence (default: hidden) */
  function setHidden(h) {
    dialog.classList.toggle('hidden', h);
    toggle.classList.toggle('dialog-open', !h);
    try { localStorage.setItem(LS_HIDDEN, h ? '1' : '0'); } catch (e) {}
    if (!h) { setTimeout(function(){ try { ta.focus(); } catch (e) {} }, 60); }
  }
  var initialHidden = true;
  try {
    var stored = localStorage.getItem(LS_HIDDEN);
    if (stored === '0') initialHidden = false;
  } catch (e) {}
  setHidden(initialHidden);

  toggle.addEventListener('click', function(e) {
    e.preventDefault();
    setHidden(false);
    refreshAgentList();
  });
  closeBtn.addEventListener('click', function(e) {
    e.preventDefault();
    setHidden(true);
  });

  /* drag-to-reposition (mirrors rc-feed pattern) */
  function applyPos(p) {
    if (!p) return;
    dialog.style.left = p.left + 'px';
    dialog.style.top  = p.top  + 'px';
    dialog.style.right = 'auto';
    dialog.style.bottom = 'auto';
  }
  try {
    var savedPos = localStorage.getItem(LS_POS);
    if (savedPos) applyPos(JSON.parse(savedPos));
  } catch (e) {}

  var dragging = false, dragOffX = 0, dragOffY = 0;
  bar.addEventListener('mousedown', function(e) {
    if (e.target.closest('#task-dialog-actions')) return;
    dragging = true;
    var r = dialog.getBoundingClientRect();
    dragOffX = e.clientX - r.left;
    dragOffY = e.clientY - r.top;
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var L = Math.max(0, Math.min(window.innerWidth  - 80, e.clientX - dragOffX));
    var T = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - dragOffY));
    applyPos({ left: L, top: T });
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    var r = dialog.getBoundingClientRect();
    try {
      localStorage.setItem(LS_POS, JSON.stringify({
        left: Math.round(r.left), top: Math.round(r.top)
      }));
    } catch (e) {}
  });

  /* agent <select> populated from window._agents (cached by /api/agents poll) */
  function refreshAgentList() {
    var agents = (window._agents || []).slice();
    var preserve = sel.value || '';
    try {
      var lastUsed = localStorage.getItem(LS_AGENT) || '';
      if (!preserve) preserve = lastUsed;
    } catch (e) {}

    if (!agents.length) {
      sel.innerHTML = '<option value="">No agents loaded yet…</option>';
      return;
    }

    /* online first, then offline (greyed) */
    function isOnline(a) {
      var s = (a.monitor_status || '').toLowerCase();
      return s === 'alive' || s === 'ok' || s === 'running' || a.alive === true;
    }
    var online  = agents.filter(isOnline);
    var offline = agents.filter(function(a){ return !isOnline(a); });
    online.sort(function(a, b){ return a.name.localeCompare(b.name); });
    offline.sort(function(a, b){ return a.name.localeCompare(b.name); });

    var html = '';
    if (online.length) {
      html += '<optgroup label="Online">';
      online.forEach(function(a) {
        html += '<option value="' + a.name + '">' + a.name + '</option>';
      });
      html += '</optgroup>';
    }
    if (offline.length) {
      html += '<optgroup label="Offline">';
      offline.forEach(function(a) {
        html += '<option value="' + a.name + '" class="offline">' + a.name + ' (offline)</option>';
      });
      html += '</optgroup>';
    }
    sel.innerHTML = html;
    if (preserve) {
      var match = Array.prototype.find.call(sel.options, function(o){ return o.value === preserve; });
      if (match) sel.value = preserve;
    }
  }
  /* keep the list fresh whenever new agent data arrives */
  setInterval(refreshAgentList, 4000);
  refreshAgentList();

  /* textarea auto-grow up to ~10 rows */
  ta.addEventListener('input', function() {
    ta.style.height = 'auto';
    ta.style.height = Math.min(280, ta.scrollHeight) + 'px';
  });

  /* status helper */
  var statusTimer = null;
  function setStatus(msg, kind) {
    status.textContent = msg || '—';
    status.className = '';
    if (kind) status.classList.add(kind);
    if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
    if (kind === 'ok') {
      statusTimer = setTimeout(function(){ status.textContent = '—'; status.className = ''; }, 4000);
    }
  }

  /* submit */
  function delegate() {
    var name = (sel.value || '').trim();
    var text = (ta.value || '').trim();
    if (!name) { setStatus('Pick an agent first.', 'error'); return; }
    if (!text) { setStatus('Task cannot be empty.', 'error'); ta.focus(); return; }
    btn.disabled = true;
    setStatus('Sending…');
    fetch('/api/task/delegate', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name: name, text: text })
    }).then(function(r){
      return r.json().then(function(j){ return { ok: r.ok, body: j }; });
    }).then(function(res) {
      btn.disabled = false;
      if (!res.ok) {
        setStatus('Error: ' + (res.body.error || 'unknown'), 'error');
        return;
      }
      setStatus('Posted to ' + (res.body.channel || ('#' + name)), 'ok');
      ta.value = '';
      ta.style.height = 'auto';
      try { localStorage.setItem(LS_AGENT, name); } catch (e) {}
      /* nudge the agent list / dispatch counter to refresh sooner */
      if (typeof window.refreshAgents === 'function') {
        setTimeout(window.refreshAgents, 800);
      }
    }).catch(function(err) {
      btn.disabled = false;
      setStatus('Network error: ' + err, 'error');
    });
  }
  btn.addEventListener('click', delegate);

  /* keyboard: Cmd/Ctrl+Enter submits, Esc closes */
  ta.addEventListener('keydown', function(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      delegate();
    }
  });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && !dialog.classList.contains('hidden')) {
      var t = e.target;
      if (t === ta || t === sel || t === btn || dialog.contains(t)) {
        setHidden(true);
      }
    }
  });
})();

/* ── Task Planner ───────────────────────────────────────────────────── */
(function() {
  var panel        = document.getElementById('planner-panel');
  var toggle       = document.getElementById('planner-toggle');
  var bar          = document.getElementById('planner-bar');
  var closeBtn     = document.getElementById('planner-close-btn');
  var newProj      = document.getElementById('planner-newproj-btn');
  var staffBtn     = document.getElementById('planner-staff-btn');
  var drawer       = document.getElementById('planner-staff-drawer');
  var staffListEl  = document.getElementById('planner-staff-list');
  var staffAddInp  = document.getElementById('planner-staff-add-input');
  var staffSuggest = document.getElementById('planner-staff-suggest');
  var digestTime   = document.getElementById('planner-digest-time');
  var digestEnable = document.getElementById('planner-digest-enabled');
  var digestLastEl = document.getElementById('planner-digest-last');
  var nextDigestEl = document.getElementById('planner-next-digest');
  var addInp       = document.getElementById('planner-add-input');
  var addBtn       = document.getElementById('planner-add-btn');
  var listEl       = document.getElementById('planner-list');
  var projSel      = document.getElementById('planner-project-filter');
  var assigneeSel  = document.getElementById('planner-assignee-filter');
  var status       = document.getElementById('planner-status');
  var countEl      = document.getElementById('planner-count');
  var badgeEl      = document.getElementById('planner-badge');
  var cleanBtn     = document.getElementById('planner-cleanup-btn');
  var chips        = panel.querySelectorAll('.planner-chip');
  if (!panel || !toggle) return;

  var LS_HIDDEN     = 'jv_planner_hidden';
  var LS_POS        = 'jv_planner_pos';
  var LS_FILTER     = 'jv_planner_filter';
  var LS_PROJ       = 'jv_planner_project';
  var LS_ASSIGNEE   = 'jv_planner_assignee';
  var LS_EXPAND     = 'jv_planner_expanded';
  var LS_DRAWER     = 'jv_planner_drawer_open';

  var state      = { projects: [], tasks: [], staff: [], settings: {} };
  var staffData  = { staff: [], settings: {} };  // last response from /staff
  var filter     = 'all';
  var projFilt   = '';
  var assignFilt = '';
  var expanded   = {}; // task id → true if currently open

  try { filter     = localStorage.getItem(LS_FILTER)   || 'all'; } catch(e){}
  try { projFilt   = localStorage.getItem(LS_PROJ)     || '';    } catch(e){}
  try { assignFilt = localStorage.getItem(LS_ASSIGNEE) || '';    } catch(e){}
  try { expanded   = JSON.parse(localStorage.getItem(LS_EXPAND) || '{}') || {}; } catch(e){}

  /* ── visibility (default: hidden) + drag persistence ─────────────── */
  function setHidden(h) {
    panel.classList.toggle('hidden', h);
    toggle.classList.toggle('panel-open', !h);
    try { localStorage.setItem(LS_HIDDEN, h ? '1' : '0'); } catch(e){}
    if (!h) {
      setTimeout(function(){ try { addInp.focus(); } catch(e){} }, 60);
      load();
    }
  }
  var initialHidden = true;
  try { if (localStorage.getItem(LS_HIDDEN) === '0') initialHidden = false; } catch(e){}
  setHidden(initialHidden);

  toggle.addEventListener('click', function(e) {
    e.preventDefault();
    setHidden(false);
  });
  closeBtn.addEventListener('click', function(e) {
    e.preventDefault();
    setHidden(true);
  });

  function applyPos(p) {
    if (!p) return;
    panel.style.left  = p.left + 'px';
    panel.style.top   = p.top  + 'px';
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
  }
  try {
    var saved = localStorage.getItem(LS_POS);
    if (saved) applyPos(JSON.parse(saved));
  } catch(e){}

  var dragging = false, dragOffX = 0, dragOffY = 0;
  bar.addEventListener('mousedown', function(e) {
    if (e.target.closest('#planner-actions')) return;
    dragging = true;
    var r = panel.getBoundingClientRect();
    dragOffX = e.clientX - r.left;
    dragOffY = e.clientY - r.top;
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var L = Math.max(0, Math.min(window.innerWidth  - 80, e.clientX - dragOffX));
    var T = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - dragOffY));
    applyPos({ left: L, top: T });
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    var r = panel.getBoundingClientRect();
    try {
      localStorage.setItem(LS_POS, JSON.stringify({
        left: Math.round(r.left), top: Math.round(r.top)
      }));
    } catch(e){}
  });

  /* ── tiny helpers ─────────────────────────────────────────────────── */
  var statusTimer = null;
  function setStatus(msg, kind) {
    status.textContent = msg || '—';
    status.className = '';
    if (kind) status.classList.add(kind);
    if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
    if (kind === 'ok') {
      statusTimer = setTimeout(function(){ status.textContent = '—'; status.className = ''; }, 3000);
    }
  }
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function todayISO() {
    var d = new Date();
    var pad = function(n){ return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
  }
  function addDays(base, n) {
    var d = new Date(base + 'T12:00:00');
    d.setDate(d.getDate() + n);
    var pad = function(x){ return x < 10 ? '0' + x : '' + x; };
    return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
  }
  function dueLabel(due) {
    if (!due) return '';
    if (due === todayISO()) return 'today';
    if (due === addDays(todayISO(), 1)) return 'tomorrow';
    if (due === addDays(todayISO(), -1)) return 'yesterday';
    /* short label for soon dates: "Fri" | "Apr 30" */
    var d = new Date(due + 'T12:00:00');
    var diff = Math.round((d - new Date(todayISO() + 'T12:00:00')) / 86400000);
    if (diff > 0 && diff < 7) {
      return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()];
    }
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return months[d.getMonth()] + ' ' + d.getDate();
  }
  function isOverdue(t) {
    return t.due && t.due < todayISO() && t.status !== 'done';
  }
  function projectById(pid) {
    return state.projects.find(function(p){ return p.id === pid; }) || state.projects[0];
  }

  /* ── smart-add parser ─────────────────────────────────────────────────
     Examples:
       "Send invoice to Bree3 #clients !high due:fri"
         → title: "Send invoice to Bree3"
         → project (matched by name fragment): clients
         → priority: 3, due: this Fri's date
       "Buy groceries due:tomorrow"
       "Ship feature !med @supervisor"
  */
  function parseQuickAdd(raw) {
    var out = { title: raw, project: null, priority: 0, due: null, assignee: null };
    var s = raw;

    /* priority: !high | !med | !low | !1 !2 !3 */
    s = s.replace(/(?:^|\s)!(high|hi|h|3)\b/i,  function(){ out.priority = 3; return ' '; });
    s = s.replace(/(?:^|\s)!(med|m|2)\b/i,      function(){ out.priority = 2; return ' '; });
    s = s.replace(/(?:^|\s)!(low|lo|l|1)\b/i,   function(){ out.priority = 1; return ' '; });

    /* due:<spec> */
    s = s.replace(/(?:^|\s)due:([^\s]+)/i, function(_, spec) {
      var lc = spec.toLowerCase();
      if (lc === 'today')      out.due = todayISO();
      else if (lc === 'tom' || lc === 'tomorrow') out.due = addDays(todayISO(), 1);
      else if (/^(mon|tue|wed|thu|fri|sat|sun)/i.test(lc)) {
        var idx = ['sun','mon','tue','wed','thu','fri','sat'].indexOf(lc.slice(0,3));
        var today = new Date(todayISO() + 'T12:00:00');
        var diff = (idx - today.getDay() + 7) % 7;
        if (diff === 0) diff = 7;
        out.due = addDays(todayISO(), diff);
      } else if (/^\d{4}-\d{2}-\d{2}$/.test(spec)) {
        out.due = spec;
      } else if (/^\+\d+$/.test(spec)) {
        out.due = addDays(todayISO(), parseInt(spec.slice(1), 10));
      }
      return ' ';
    });

    /* #project (match by fragment, case-insensitive) */
    s = s.replace(/(?:^|\s)#([\w-]+)/i, function(_, frag) {
      var match = state.projects.find(function(p){
        return p.name.toLowerCase().indexOf(frag.toLowerCase()) >= 0;
      });
      if (match) out.project = match.id;
      return ' ';
    });

    /* @name — resolve in order: staff (username/name) → agents → raw */
    s = s.replace(/(?:^|\s)@([\w.-]+)/i, function(_, name) {
      var nl = name.toLowerCase();
      var staff = (state.staff || []).find(function(u) {
        return u.username.toLowerCase() === nl
            || (u.name || '').toLowerCase() === nl;
      });
      if (staff) { out.assignee = staff.username; return ' '; }
      var agents = window._agents || [];
      var a = agents.find(function(x){ return x.name.toLowerCase() === nl; });
      out.assignee = a ? a.name : name;
      return ' ';
    });

    out.title = s.replace(/\s+/g, ' ').trim();
    return out;
  }

  /* who-pill class + dot color: agent if matches an agents/ folder. */
  function isAgentName(name) {
    if (!name) return false;
    var agents = window._agents || [];
    return agents.some(function(a){ return a.name.toLowerCase() === name.toLowerCase(); });
  }
  function whoDisplayName(username) {
    if (!username) return '';
    var staff = (state.staff || []).find(function(u){
      return u.username.toLowerCase() === username.toLowerCase();
    });
    return staff ? (staff.name || staff.username) : username;
  }

  /* ── render ───────────────────────────────────────────────────────── */
  function activeCount() {
    return state.tasks.filter(function(t){ return t.status !== 'done'; }).length;
  }
  function refreshBadge() {
    var n = activeCount();
    badgeEl.textContent = n > 99 ? '99+' : String(n);
    badgeEl.classList.toggle('zero', n === 0);
    countEl.textContent = n + ' active';
  }
  function refreshProjectFilter() {
    var prev = projSel.value || projFilt || '';
    var html = '<option value="">All projects</option>';
    state.projects.slice().sort(function(a,b){ return (a.order||0)-(b.order||0); })
      .forEach(function(p) {
        html += '<option value="' + p.id + '">' + escapeHtml(p.name) + '</option>';
      });
    projSel.innerHTML = html;
    if (prev) projSel.value = prev;
  }
  /* Assignee filter: build buckets from open tasks + staff list, sorted
     by open count desc so the heaviest workload sits near the top. */
  function refreshAssigneeFilter() {
    var prev = assigneeSel.value || assignFilt || '';
    var open = {};
    var unassigned = 0;
    state.tasks.forEach(function(t) {
      if (t.status === 'done') return;
      if (!t.assignee) { unassigned++; return; }
      open[t.assignee] = (open[t.assignee] || 0) + 1;
    });
    /* include known staff even with 0 open tasks so the menu lists everyone */
    (state.staff || []).forEach(function(s){
      if (open[s.username] === undefined) open[s.username] = 0;
    });
    var entries = Object.keys(open).map(function(name){
      return { name: name, count: open[name] };
    });
    entries.sort(function(a,b){
      if (a.count !== b.count) return b.count - a.count;
      return a.name.localeCompare(b.name);
    });
    var html = '<option value="">All assignees</option>';
    html += '<option value="__unassigned">Unassigned (' + unassigned + ')</option>';
    entries.forEach(function(e) {
      var disp = whoDisplayName(e.name);
      var lbl  = (disp && disp !== e.name)
        ? (e.name + ' \u2014 ' + disp + ' (' + e.count + ')')
        : (e.name + ' (' + e.count + ')');
      html += '<option value="' + escapeHtml(e.name) + '">' + escapeHtml(lbl) + '</option>';
    });
    assigneeSel.innerHTML = html;
    if (prev) {
      var match = Array.prototype.find.call(assigneeSel.options, function(o){ return o.value === prev; });
      if (match) assigneeSel.value = prev;
    }
  }
  function passesFilter(t) {
    if (projFilt && t.project !== projFilt) return false;
    if (assignFilt) {
      if (assignFilt === '__unassigned') {
        if (t.assignee) return false;
      } else if ((t.assignee || '').toLowerCase() !== assignFilt.toLowerCase()) {
        return false;
      }
    }
    if (filter === 'today') {
      return t.status !== 'done' && (t.due === todayISO() || isOverdue(t));
    }
    if (filter === 'active') return t.status !== 'done';
    if (filter === 'done')   return t.status === 'done';
    return true;
  }
  /* sort: doing first, then todo (due-asc, prio-desc), then done (newest first) */
  function compareTasks(a, b) {
    var rank = function(s){ return s === 'doing' ? 0 : s === 'todo' ? 1 : 2; };
    var rd = rank(a.status) - rank(b.status);
    if (rd) return rd;
    if (a.status === 'done') {
      return (b.completed_at || '').localeCompare(a.completed_at || '');
    }
    /* due: nulls last, earliest first */
    var ad = a.due || '9999-12-31', bd = b.due || '9999-12-31';
    if (ad !== bd) return ad < bd ? -1 : 1;
    if (a.priority !== b.priority) return b.priority - a.priority;
    return (a.created_at || '').localeCompare(b.created_at || '');
  }

  function renderTask(t) {
    var proj  = projectById(t.project);
    var color = (proj && proj.color) || '#fbbf24';
    var prioLabels = ['', 'L', 'M', 'H'];
    var classes = ['planner-task', 'is-' + t.status];
    if (isOverdue(t)) classes.push('is-overdue');
    if (expanded[t.id]) classes.push('expanded');

    var meta = '';
    if (proj && proj.id !== 'p-inbox') {
      meta += '<span class="pill proj-pill"><span class="proj-dot" style="background:' +
              escapeHtml(color) + '"></span>' + escapeHtml(proj.name) + '</span>';
    }
    if (t.due) {
      meta += '<span class="pill due-pill">' + escapeHtml(dueLabel(t.due)) + '</span>';
    }
    if (t.priority > 0) {
      meta += '<span class="pill prio-pill p' + t.priority + '">' +
              prioLabels[t.priority] + '</span>';
    }
    if (t.assignee) {
      var who = whoDisplayName(t.assignee);
      var isAg = isAgentName(t.assignee);
      meta += '<span class="pill who-pill' + (isAg ? ' is-agent' : '') + '">' +
              '<span class="who-dot"></span>@' + escapeHtml(t.assignee) +
              (who && who !== t.assignee ? ' <span style="opacity:0.7">(' + escapeHtml(who) + ')</span>' : '') +
              '</span>';
    }

    /* edit row only rendered when expanded — keeps DOM lean */
    var edit = '';
    if (expanded[t.id]) {
      var projOpts = state.projects.slice().sort(function(a,b){ return (a.order||0)-(b.order||0); })
        .map(function(p) {
          return '<option value="' + p.id + '"' + (p.id === t.project ? ' selected' : '') + '>' +
                 escapeHtml(p.name) + '</option>';
        }).join('');
      var prioOpts = ['None','Low','Med','High'].map(function(lbl, i) {
        return '<option value="' + i + '"' + (i === t.priority ? ' selected' : '') + '>' + lbl + '</option>';
      }).join('');

      /* Grouped assignee select: Staff (workload-sorted), then Agents.
         Show the current value as a custom option if it's not in either list
         (e.g. legacy free-text strings). */
      var workloadStaff = (state.staff || []).slice().map(function(s) {
        return { username: s.username, name: s.name, open: 0 };
      });
      var workMap = {};
      state.tasks.forEach(function(x) {
        if (x.status === 'done' || !x.assignee) return;
        workMap[x.assignee] = (workMap[x.assignee] || 0) + 1;
      });
      workloadStaff.forEach(function(s){ s.open = workMap[s.username] || 0; });
      workloadStaff.sort(function(a,b){
        if (a.open !== b.open) return b.open - a.open;
        return a.username.localeCompare(b.username);
      });
      var staffOpts = workloadStaff.map(function(s){
        var sel = (s.username === t.assignee) ? ' selected' : '';
        var lbl = (s.name || s.username) + ' (' + s.open + ')';
        return '<option value="' + escapeHtml(s.username) + '"' + sel + '>' + escapeHtml(lbl) + '</option>';
      }).join('');
      var agents = (window._agents || []).slice().sort(function(a,b){
        return a.name.localeCompare(b.name);
      });
      var agentOpts = agents.map(function(a){
        var sel = (a.name === t.assignee) ? ' selected' : '';
        return '<option value="' + escapeHtml(a.name) + '"' + sel + '>' + escapeHtml(a.name) + '</option>';
      }).join('');
      var unknownOpt = '';
      if (t.assignee && !workloadStaff.some(function(s){ return s.username === t.assignee; })
          && !agents.some(function(a){ return a.name === t.assignee; })) {
        unknownOpt = '<option value="' + escapeHtml(t.assignee) + '" selected>' +
                     escapeHtml(t.assignee) + ' (other)</option>';
      }
      var assigneeSelectHtml =
        '<select class="edit-assignee grow">' +
          '<option value=""' + (t.assignee ? '' : ' selected') + '>— unassigned —</option>' +
          unknownOpt +
          (staffOpts ? '<optgroup label="Staff">' + staffOpts + '</optgroup>' : '') +
          (agentOpts ? '<optgroup label="Agents">' + agentOpts + '</optgroup>' : '') +
        '</select>';

      edit =
        '<div class="planner-edit" data-id="' + t.id + '">' +
          '<textarea class="edit-notes" placeholder="Notes…">' + escapeHtml(t.notes || '') + '</textarea>' +
          '<div class="planner-edit-row">' +
            '<label>Project</label>' +
            '<select class="edit-project">' + projOpts + '</select>' +
            '<label>Priority</label>' +
            '<select class="edit-priority">' + prioOpts + '</select>' +
          '</div>' +
          '<div class="planner-edit-row">' +
            '<label>Due</label>' +
            '<input type="date" class="edit-due" value="' + escapeHtml(t.due || '') + '"/>' +
            '<label>Assignee</label>' +
            assigneeSelectHtml +
          '</div>' +
          '<div class="planner-edit-actions">' +
            '<button class="danger" data-act="delete">Delete</button>' +
            '<button class="save"   data-act="save">Save</button>' +
          '</div>' +
        '</div>';
    }

    return (
      '<div class="' + classes.join(' ') + '" data-id="' + t.id + '">' +
        '<div class="planner-check ' + t.status + '" data-act="cycle" title="Click to advance status">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>' +
        '</div>' +
        '<div class="planner-body" data-act="expand">' +
          '<div class="planner-title">' + escapeHtml(t.title) + '</div>' +
          (meta ? '<div class="planner-meta">' + meta + '</div>' : '') +
        '</div>' +
        edit +
      '</div>'
    );
  }

  function render() {
    refreshProjectFilter();
    refreshAssigneeFilter();
    refreshBadge();

    /* highlight active filter chip */
    chips.forEach(function(c){
      c.classList.toggle('active', c.getAttribute('data-filter') === filter);
    });
    projSel.value     = projFilt;
    assigneeSel.value = assignFilt;

    var visible = state.tasks.filter(passesFilter).sort(compareTasks);
    if (!visible.length) {
      var msg = state.tasks.length
        ? 'Nothing matches this filter.'
        : 'No tasks yet — add one above.';
      listEl.innerHTML =
        '<div class="planner-empty"><div class="big">·</div>' + escapeHtml(msg) + '</div>';
      return;
    }
    listEl.innerHTML = visible.map(renderTask).join('');
  }

  /* ── server I/O ───────────────────────────────────────────────────── */
  function load() {
    return fetch('/api/planner/state').then(function(r){ return r.json(); })
      .then(function(j) {
        state = j; render();
      })
      .catch(function(err){ setStatus('Load failed: ' + err, 'error'); });
  }
  function api(method, url, body) {
    var opts = { method: method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function(r) {
      return r.json().then(function(j){ return { ok: r.ok, body: j }; });
    });
  }
  function persistExpanded() {
    try { localStorage.setItem(LS_EXPAND, JSON.stringify(expanded)); } catch(e){}
  }

  /* ── add task ─────────────────────────────────────────────────────── */
  function addTask() {
    var raw = (addInp.value || '').trim();
    if (!raw) return;
    var parsed = parseQuickAdd(raw);
    if (!parsed.title) { setStatus('Title cannot be empty.', 'error'); return; }
    /* default to currently filtered project if any (and parser didn't override) */
    if (!parsed.project && projFilt) parsed.project = projFilt;
    addBtn.disabled = true;
    api('POST', '/api/planner/task', parsed).then(function(res) {
      addBtn.disabled = false;
      if (!res.ok) { setStatus(res.body.error || 'add failed', 'error'); return; }
      addInp.value = '';
      state.tasks.push(res.body.task);
      render();
      setStatus('Added.', 'ok');
    }).catch(function(err) {
      addBtn.disabled = false;
      setStatus('Network error: ' + err, 'error');
    });
  }
  addBtn.addEventListener('click', addTask);
  addInp.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); addTask(); }
  });

  /* ── status cycle: todo → doing → done → todo ─────────────────────── */
  function cycle(id) {
    var t = state.tasks.find(function(x){ return x.id === id; });
    if (!t) return;
    var next = t.status === 'todo' ? 'doing' : t.status === 'doing' ? 'done' : 'todo';
    /* optimistic */
    t.status = next;
    if (next === 'done') t.completed_at = new Date().toISOString();
    else if (next !== 'done') t.completed_at = null;
    render();
    api('PATCH', '/api/planner/task/' + encodeURIComponent(id), { status: next })
      .then(function(res) {
        if (!res.ok) { setStatus(res.body.error || 'update failed', 'error'); load(); }
      });
  }

  /* ── expand/collapse + edit ───────────────────────────────────────── */
  function toggleExpand(id) {
    if (expanded[id]) delete expanded[id];
    else expanded[id] = true;
    persistExpanded();
    render();
  }
  function saveEdit(id, row) {
    var patch = {
      notes:    row.querySelector('.edit-notes').value,
      project:  row.querySelector('.edit-project').value,
      priority: parseInt(row.querySelector('.edit-priority').value, 10),
      due:      row.querySelector('.edit-due').value || null,
      assignee: row.querySelector('.edit-assignee').value || null,
    };
    api('PATCH', '/api/planner/task/' + encodeURIComponent(id), patch).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'save failed', 'error'); return; }
      Object.assign(state.tasks.find(function(x){ return x.id === id; }) || {}, res.body.task);
      delete expanded[id];
      persistExpanded();
      render();
      setStatus('Saved.', 'ok');
    });
  }
  function deleteTask(id) {
    if (!confirm('Delete this task?')) return;
    api('DELETE', '/api/planner/task/' + encodeURIComponent(id)).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'delete failed', 'error'); return; }
      state.tasks = state.tasks.filter(function(t){ return t.id !== id; });
      delete expanded[id];
      render();
      setStatus('Deleted.', 'ok');
    });
  }

  /* delegated click handler on the task list */
  listEl.addEventListener('click', function(e) {
    var taskEl = e.target.closest('.planner-task');
    if (!taskEl) return;
    var id  = taskEl.getAttribute('data-id');
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var act = btn.getAttribute('data-act');
    if (act === 'cycle')  { cycle(id); }
    else if (act === 'expand') { toggleExpand(id); }
    else if (act === 'save')   { saveEdit(id, taskEl.querySelector('.planner-edit')); }
    else if (act === 'delete') { deleteTask(id); }
  });
  /* expanded title double-click → quick rename */
  listEl.addEventListener('dblclick', function(e) {
    var titleEl = e.target.closest('.planner-title');
    if (!titleEl) return;
    var taskEl = titleEl.closest('.planner-task');
    var id     = taskEl.getAttribute('data-id');
    var t = state.tasks.find(function(x){ return x.id === id; });
    if (!t) return;
    var v = prompt('Edit title:', t.title);
    if (v == null) return;
    v = v.trim(); if (!v) return;
    api('PATCH', '/api/planner/task/' + encodeURIComponent(id), { title: v }).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'rename failed', 'error'); return; }
      t.title = res.body.task.title;
      render();
    });
  });

  /* ── filters & project chooser ────────────────────────────────────── */
  chips.forEach(function(c) {
    c.addEventListener('click', function() {
      filter = c.getAttribute('data-filter');
      try { localStorage.setItem(LS_FILTER, filter); } catch(e){}
      render();
    });
  });
  projSel.addEventListener('change', function() {
    projFilt = projSel.value || '';
    try { localStorage.setItem(LS_PROJ, projFilt); } catch(e){}
    render();
  });
  assigneeSel.addEventListener('change', function() {
    assignFilt = assigneeSel.value || '';
    try { localStorage.setItem(LS_ASSIGNEE, assignFilt); } catch(e){}
    render();
  });

  /* ── new project ──────────────────────────────────────────────────── */
  newProj.addEventListener('click', function() {
    var name = prompt('New project name:');
    if (!name) return;
    name = name.trim();
    if (!name) return;
    /* random pleasant color */
    var palette = ['#fbbf24','#a78bfa','#38bdf8','#22c55e','#f472b6','#06b6d4','#fb923c','#84cc16'];
    var color = palette[state.projects.length % palette.length];
    api('POST', '/api/planner/project', { name: name, color: color }).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'create failed', 'error'); return; }
      state.projects.push(res.body.project);
      projFilt = res.body.project.id;
      try { localStorage.setItem(LS_PROJ, projFilt); } catch(e){}
      render();
      setStatus('Project added.', 'ok');
    });
  });

  /* ── clear-done bulk action ───────────────────────────────────────── */
  cleanBtn.addEventListener('click', function() {
    var done = state.tasks.filter(function(t){ return t.status === 'done'; });
    if (!done.length) { setStatus('Nothing to clear.', 'ok'); return; }
    if (!confirm('Remove ' + done.length + ' completed task(s)?')) return;
    /* sequential to keep things simple — these are rare bulk ops */
    var rest = done.slice();
    function next() {
      if (!rest.length) {
        load();
        setStatus('Cleared.', 'ok');
        return;
      }
      var t = rest.shift();
      api('DELETE', '/api/planner/task/' + encodeURIComponent(t.id)).then(next);
    }
    next();
  });

  /* ── keyboard ─────────────────────────────────────────────────────── */
  document.addEventListener('keydown', function(e) {
    /* Escape closes the panel when its own input has focus */
    if (e.key === 'Escape' && !panel.classList.contains('hidden')) {
      var t = e.target;
      if (t === addInp || t === projSel || panel.contains(t)) {
        setHidden(true);
      }
    }
  });

  /* ── Staff drawer + RC integration ───────────────────────────────── */
  function setDrawer(open) {
    drawer.classList.toggle('open', open);
    try { localStorage.setItem(LS_DRAWER, open ? '1' : '0'); } catch(e){}
    if (open) {
      loadStaff();
      setTimeout(function(){ try { staffAddInp.focus(); } catch(e){} }, 220);
    } else {
      hideSuggest();
    }
  }
  staffBtn.addEventListener('click', function() {
    setDrawer(!drawer.classList.contains('open'));
  });

  function fmtLastSent(iso) {
    if (!iso) return 'never';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      var pad = function(n){ return n < 10 ? '0' + n : '' + n; };
      var t   = pad(d.getHours()) + ':' + pad(d.getMinutes());
      var ago = Math.round((Date.now() - d.getTime()) / 60000);
      if (ago < 1) return 'just now';
      if (ago < 60) return ago + 'm ago';
      if (ago < 60 * 24) return Math.round(ago/60) + 'h ago';
      return d.toLocaleDateString() + ' ' + t;
    } catch(e) { return iso; }
  }

  function renderStaffRow(s) {
    var disp  = s.name && s.name !== s.username ? s.name : s.username;
    var badge = '<span class="open-badge' + (s.open ? '' : ' zero') + '">' + s.open + '</span>';
    return (
      '<div class="staff-row" data-username="' + escapeHtml(s.username) + '">' +
        badge +
        '<div class="grow"><span class="name">' + escapeHtml(disp) + '</span>' +
          (disp !== s.username ? ' <span class="handle">@' + escapeHtml(s.username) + '</span>' : '') +
        '</div>' +
        '<button class="icon-btn" data-act="staff-send" title="Send digest now">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
        '</button>' +
        '<button class="icon-btn danger" data-act="staff-remove" title="Remove from staff">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
        '</button>' +
      '</div>'
    );
  }
  function renderStaffList() {
    var s = staffData.staff || [];
    if (!s.length) {
      staffListEl.innerHTML = '<div class="staff-row" style="color:var(--text2);font-size:11px;justify-content:center;">No staff yet — add a Rocket.Chat user below.</div>';
    } else {
      staffListEl.innerHTML = s.map(renderStaffRow).join('');
    }
  }
  function loadStaff() {
    return fetch('/api/planner/staff').then(function(r){ return r.json(); })
      .then(function(j) {
        staffData = j;
        state.staff    = j.staff || [];
        state.settings = j.settings || {};
        renderStaffList();
        applySettingsToUI();
        render();
        loadSettings();  // refresh next-digest countdown
      })
      .catch(function(err){ setStatus('Staff load failed: ' + err, 'error'); });
  }
  function applySettingsToUI() {
    var s = staffData.settings || {};
    var pad = function(n){ return n < 10 ? '0' + n : '' + n; };
    digestTime.value     = pad(s.digest_hour || 0) + ':' + pad(s.digest_minute || 0);
    digestEnable.checked = !!s.digest_enabled;
    digestLastEl.textContent = 'last sent ' + fmtLastSent(s.last_digest_date || '');
  }

  /* delegated click on staff list (send / remove) */
  staffListEl.addEventListener('click', function(e) {
    var row = e.target.closest('.staff-row');
    if (!row) return;
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var username = row.getAttribute('data-username');
    var act = btn.getAttribute('data-act');
    if (act === 'staff-send') {
      btn.disabled = true;
      fetch('/api/planner/digest/send_now?assignee=' + encodeURIComponent(username),
            { method: 'POST' })
        .then(function(r){ return r.json().then(function(j){ return { ok: r.ok, body: j }; }); })
        .then(function(res) {
          btn.disabled = false;
          if (!res.ok) setStatus('Send failed: ' + (res.body.error || 'unknown'), 'error');
          else        setStatus('Digest sent to @' + username, 'ok');
        });
    } else if (act === 'staff-remove') {
      if (!confirm('Remove @' + username + ' from staff? Their tasks stay assigned.')) return;
      api('DELETE', '/api/planner/staff/' + encodeURIComponent(username))
        .then(function(res) {
          if (!res.ok) { setStatus(res.body.error || 'remove failed', 'error'); return; }
          setStatus('Removed @' + username, 'ok');
          loadStaff();
        });
    }
  });

  /* ── Staff add: autocomplete against /users/search ──────────────── */
  var suggestState = { items: [], idx: -1 };
  var suggestTimer = null;

  function hideSuggest() {
    staffSuggest.classList.remove('open');
    staffSuggest.innerHTML = '';
    suggestState = { items: [], idx: -1 };
  }
  function renderSuggest() {
    if (!suggestState.items.length) {
      staffSuggest.innerHTML = '<div class="empty">No matches.</div>';
      staffSuggest.classList.add('open');
      return;
    }
    staffSuggest.innerHTML = suggestState.items.map(function(u, i) {
      var disp = u.name && u.name !== u.username ? u.name : u.username;
      return '<div class="row' + (i === suggestState.idx ? ' kbd' : '') + '" data-username="' + escapeHtml(u.username) + '">' +
        '<span class="name">' + escapeHtml(disp) + '</span>' +
        (disp !== u.username ? ' <span class="handle">@' + escapeHtml(u.username) + '</span>' : '') +
      '</div>';
    }).join('');
    staffSuggest.classList.add('open');
  }
  function searchUsers(q) {
    fetch('/api/planner/users/search?q=' + encodeURIComponent(q))
      .then(function(r){ return r.json(); })
      .then(function(j) {
        var existing = (staffData.staff || []).map(function(s){ return s.username.toLowerCase(); });
        var filtered = (j.users || []).filter(function(u){
          return existing.indexOf(u.username.toLowerCase()) < 0;
        });
        suggestState.items = filtered.slice(0, 8);
        suggestState.idx   = filtered.length ? 0 : -1;
        renderSuggest();
      })
      .catch(function(){ hideSuggest(); });
  }
  staffAddInp.addEventListener('input', function() {
    var q = (staffAddInp.value || '').trim();
    if (suggestTimer) clearTimeout(suggestTimer);
    if (!q) { hideSuggest(); return; }
    suggestTimer = setTimeout(function(){ searchUsers(q); }, 180);
  });
  staffAddInp.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown') {
      if (!suggestState.items.length) return;
      suggestState.idx = (suggestState.idx + 1) % suggestState.items.length;
      renderSuggest();
      e.preventDefault();
    } else if (e.key === 'ArrowUp') {
      if (!suggestState.items.length) return;
      suggestState.idx = (suggestState.idx - 1 + suggestState.items.length) % suggestState.items.length;
      renderSuggest();
      e.preventDefault();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      var picked = suggestState.items[suggestState.idx];
      var username = picked ? picked.username : (staffAddInp.value || '').trim().replace(/^@/, '');
      if (!username) return;
      addStaff(username);
    } else if (e.key === 'Escape') {
      hideSuggest();
    }
  });
  staffSuggest.addEventListener('click', function(e) {
    var row = e.target.closest('.row[data-username]');
    if (!row) return;
    addStaff(row.getAttribute('data-username'));
  });
  document.addEventListener('click', function(e) {
    if (!staffSuggest.contains(e.target) && e.target !== staffAddInp) hideSuggest();
  });

  function addStaff(username) {
    if (!username) return;
    api('POST', '/api/planner/staff', { username: username }).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'add failed', 'error'); return; }
      staffAddInp.value = '';
      hideSuggest();
      setStatus('Added @' + username + ' to staff', 'ok');
      loadStaff();
    });
  }

  /* ── Digest settings (time + enabled) ───────────────────────────── */
  function pushSettings(patch) {
    api('PATCH', '/api/planner/settings', patch).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'settings failed', 'error'); return; }
      staffData.settings = res.body.settings;
      state.settings     = res.body.settings;
      applySettingsToUI();
      loadSettings();
    });
  }
  digestTime.addEventListener('change', function() {
    var parts = (digestTime.value || '08:00').split(':');
    pushSettings({
      digest_hour:   parseInt(parts[0], 10) || 0,
      digest_minute: parseInt(parts[1], 10) || 0,
    });
  });
  digestEnable.addEventListener('change', function() {
    pushSettings({ digest_enabled: !!digestEnable.checked });
  });

  /* ── Footer: next-digest countdown ──────────────────────────────── */
  var nextDigestAt = null;
  function loadSettings() {
    return fetch('/api/planner/settings').then(function(r){ return r.json(); })
      .then(function(j) {
        nextDigestAt = j.next_digest_at ? new Date(j.next_digest_at) : null;
        renderCountdown();
      })
      .catch(function(){});
  }
  function renderCountdown() {
    if (!nextDigestEl) return;
    if (!nextDigestAt || isNaN(nextDigestAt.getTime())) {
      nextDigestEl.textContent = '';
      return;
    }
    var ms = nextDigestAt - new Date();
    if (ms <= 0) { nextDigestEl.textContent = 'Digest firing now…'; return; }
    var mins = Math.round(ms / 60000);
    var hh   = Math.floor(mins / 60);
    var mm   = mins % 60;
    var when;
    if (hh > 24) when = Math.round(hh/24) + 'd';
    else if (hh > 0) when = hh + 'h ' + mm + 'm';
    else when = mm + 'm';
    var pad = function(n){ return n < 10 ? '0' + n : '' + n; };
    var clock = pad(nextDigestAt.getHours()) + ':' + pad(nextDigestAt.getMinutes());
    nextDigestEl.textContent = 'Next digest ' + clock + ' (' + when + ')';
  }
  setInterval(renderCountdown, 30 * 1000);

  /* ── boot + soft refresh while open ───────────────────────────────── */
  /* restore drawer open/closed */
  try { if (localStorage.getItem(LS_DRAWER) === '1') drawer.classList.add('open'); } catch(e){}
  if (!panel.classList.contains('hidden')) load();
  /* poll every 30s while panel is open so multiple browsers stay roughly in sync */
  setInterval(function() {
    if (!panel.classList.contains('hidden') && !document.hidden) {
      load();
      if (drawer.classList.contains('open')) loadStaff();
      loadSettings();
    }
  }, 30000);
  /* always populate the badge + settings on first paint, even if panel never opened */
  load();
  loadStaff();
  loadSettings();
})();

/* ── Calendar widget ─────────────────────────────────────────────────── */
(function() {
  var panel    = document.getElementById('calendar-panel');
  var toggle   = document.getElementById('calendar-toggle');
  var bar      = document.getElementById('calendar-bar');
  var closeBtn = document.getElementById('calendar-close-btn');
  var prevBtn  = document.getElementById('calendar-prev');
  var nextBtn  = document.getElementById('calendar-next');
  var todayBtn = document.getElementById('calendar-today-btn');
  var labelEl  = document.getElementById('calendar-month-label');
  var gridEl   = document.getElementById('calendar-grid');
  var detailEl = document.getElementById('calendar-day-detail');
  var statusEl = document.getElementById('calendar-status');
  var badgeEl  = document.getElementById('calendar-badge');
  if (!panel || !toggle) return;

  var LS_HIDDEN   = 'jv_cal_hidden';
  var LS_POS      = 'jv_cal_pos';
  var LS_SELECTED = 'jv_cal_selected';

  var view     = { year: 0, month: 0 };  // 0-indexed month
  var events   = [];                      // calendar events
  var tasks    = [];                      // planner tasks (overlay)
  var selected = null;                    // YYYY-MM-DD or null
  var editing  = null;                    // event id being inline-edited

  function pad(n)        { return n < 10 ? '0' + n : '' + n; }
  function ymd(d)        { return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()); }
  function todayStr()    { return ymd(new Date()); }
  function fromYmd(s)    { return new Date(s + 'T12:00:00'); }
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ── visibility + drag persistence ───────────────────────────────── */
  function setHidden(h) {
    panel.classList.toggle('hidden', h);
    toggle.classList.toggle('panel-open', !h);
    try { localStorage.setItem(LS_HIDDEN, h ? '1' : '0'); } catch(e){}
    if (!h) load();
  }
  var initialHidden = true;
  try { if (localStorage.getItem(LS_HIDDEN) === '0') initialHidden = false; } catch(e){}
  setHidden(initialHidden);

  toggle.addEventListener('click', function(e) {
    e.preventDefault();
    setHidden(false);
  });
  closeBtn.addEventListener('click', function(e) {
    e.preventDefault();
    setHidden(true);
  });

  function applyPos(p) {
    if (!p) return;
    panel.style.left  = p.left + 'px';
    panel.style.top   = p.top  + 'px';
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
  }
  try {
    var saved = localStorage.getItem(LS_POS);
    if (saved) applyPos(JSON.parse(saved));
  } catch(e){}

  var dragging = false, dragOffX = 0, dragOffY = 0;
  bar.addEventListener('mousedown', function(e) {
    if (e.target.closest('#calendar-actions')) return;
    dragging = true;
    var r = panel.getBoundingClientRect();
    dragOffX = e.clientX - r.left;
    dragOffY = e.clientY - r.top;
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging) return;
    var L = Math.max(0, Math.min(window.innerWidth  - 80, e.clientX - dragOffX));
    var T = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - dragOffY));
    applyPos({ left: L, top: T });
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    var r = panel.getBoundingClientRect();
    try {
      localStorage.setItem(LS_POS, JSON.stringify({
        left: Math.round(r.left), top: Math.round(r.top)
      }));
    } catch(e){}
  });

  /* ── status helper ───────────────────────────────────────────────── */
  var statusTimer = null;
  function setStatus(msg, kind) {
    statusEl.textContent = msg || '—';
    statusEl.className = '';
    if (kind) statusEl.classList.add(kind);
    if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
    if (kind === 'ok') {
      statusTimer = setTimeout(function(){ statusEl.textContent = '—'; statusEl.className = ''; }, 3000);
    }
  }

  /* ── month nav ────────────────────────────────────────────────────── */
  function setMonth(year, month) {
    /* normalize roll-over */
    var d = new Date(year, month, 1);
    view.year  = d.getFullYear();
    view.month = d.getMonth();
    render();
    fetchData();
  }
  prevBtn.addEventListener('click', function(){ setMonth(view.year, view.month - 1); });
  nextBtn.addEventListener('click', function(){ setMonth(view.year, view.month + 1); });
  todayBtn.addEventListener('click', function() {
    var n = new Date();
    selected = todayStr();
    try { localStorage.setItem(LS_SELECTED, selected); } catch(e){}
    setMonth(n.getFullYear(), n.getMonth());
  });

  /* ── server I/O ───────────────────────────────────────────────────── */
  function api(method, url, body) {
    var opts = { method: method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function(r) {
      return r.json().then(function(j){ return { ok: r.ok, body: j }; });
    });
  }

  function monthRange() {
    var first = new Date(view.year, view.month, 1);
    var last  = new Date(view.year, view.month + 1, 0);
    /* widen to the 42-day display window so border weeks fetch too */
    var start = new Date(first); start.setDate(start.getDate() - first.getDay());
    var end   = new Date(last);  end.setDate(end.getDate() + (6 - last.getDay()));
    return { from: ymd(start), to: ymd(end) };
  }

  function fetchData() {
    var r = monthRange();
    var p1 = fetch('/api/calendar/events?from=' + r.from + '&to=' + r.to)
      .then(function(x){ return x.json(); })
      .then(function(j){ events = j.events || []; });
    /* overlay planner tasks (no date filter — planner is small) */
    var p2 = fetch('/api/planner/state')
      .then(function(x){ return x.json(); })
      .then(function(j){ tasks = (j.tasks || []).filter(function(t){ return t.due; }); });
    return Promise.all([p1, p2]).then(function() {
      render();
      refreshBadge();
    });
  }
  function load() {
    var n = new Date();
    if (!view.year) { view.year = n.getFullYear(); view.month = n.getMonth(); }
    if (!selected) {
      try { selected = localStorage.getItem(LS_SELECTED) || todayStr(); } catch(e){ selected = todayStr(); }
    }
    return fetchData();
  }

  /* ── badge: count of items today through the next 7 days ─────────── */
  function refreshBadge() {
    var t0 = todayStr();
    var d  = new Date(); d.setDate(d.getDate() + 7);
    var t7 = ymd(d);
    var n  = 0;
    events.forEach(function(e){ if (e.date >= t0 && e.date <= t7) n++; });
    tasks.forEach(function(x){ if (x.status !== 'done' && x.due >= t0 && x.due <= t7) n++; });
    badgeEl.textContent = n > 99 ? '99+' : String(n);
    badgeEl.classList.toggle('zero', n === 0);
  }

  /* ── render ───────────────────────────────────────────────────────── */
  var MONTHS = ['January','February','March','April','May','June',
                'July','August','September','October','November','December'];
  var DOW    = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

  function itemsByDate() {
    var map = {};
    events.forEach(function(e) {
      (map[e.date] = map[e.date] || []).push({ kind: 'event', data: e });
    });
    tasks.forEach(function(t) {
      (map[t.due] = map[t.due] || []).push({ kind: 'task', data: t });
    });
    /* sort: events with time first (chronological), then untimed events,
       then tasks (priority desc) */
    Object.keys(map).forEach(function(k) {
      map[k].sort(function(a, b) {
        if (a.kind !== b.kind) return a.kind === 'event' ? -1 : 1;
        if (a.kind === 'event') {
          var at = a.data.time || 'zz';
          var bt = b.data.time || 'zz';
          return at.localeCompare(bt);
        }
        return (b.data.priority || 0) - (a.data.priority || 0);
      });
    });
    return map;
  }

  function pillFor(it) {
    if (it.kind === 'event') {
      var label = (it.data.time ? it.data.time + ' ' : '') + it.data.title;
      return '<span class="cal-pill" title="' + escapeHtml(it.data.title) + '">' +
             escapeHtml(label) + '</span>';
    }
    var t = it.data;
    var cls = 'cal-pill is-task';
    if (t.status === 'done') cls += ' is-done';
    else if (t.due < todayStr()) cls += ' is-overdue';
    return '<span class="' + cls + '" title="' + escapeHtml(t.title) + '">' +
           escapeHtml('· ' + t.title) + '</span>';
  }

  function render() {
    labelEl.textContent = MONTHS[view.month] + ' ' + view.year;

    var map  = itemsByDate();
    var html = '';
    DOW.forEach(function(d){ html += '<div class="cal-dow">' + d + '</div>'; });

    var first    = new Date(view.year, view.month, 1);
    var startDow = first.getDay();
    var d = new Date(first);
    d.setDate(d.getDate() - startDow);
    var t0 = todayStr();
    /* always 6 weeks (42 cells) so the panel height is stable */
    for (var i = 0; i < 42; i++) {
      var key = ymd(d);
      var cls = ['cal-cell'];
      if (d.getMonth() !== view.month) cls.push('other-month');
      if (d.getDay() === 0 || d.getDay() === 6) cls.push('weekend');
      if (key === t0) cls.push('today');
      if (key === selected) cls.push('selected');

      var pills = (map[key] || []);
      var pillHtml = '';
      var max = 3;
      pills.slice(0, max).forEach(function(it){ pillHtml += pillFor(it); });
      if (pills.length > max) {
        pillHtml += '<span class="cal-more">+' + (pills.length - max) + ' more</span>';
      }

      html += '<div class="' + cls.join(' ') + '" data-date="' + key + '">' +
                '<div class="cal-day-num">' + d.getDate() + '</div>' +
                pillHtml +
              '</div>';
      d.setDate(d.getDate() + 1);
    }
    gridEl.innerHTML = html;
    renderDayDetail(map);
  }

  /* ── day-detail strip: events + tasks for `selected` ─────────────── */
  function renderDayDetail(map) {
    if (!selected) {
      detailEl.innerHTML = '<div class="cal-detail-empty">Pick a day above to see what\u2019s scheduled.</div>';
      return;
    }
    var d = fromYmd(selected);
    var pretty = DOW[d.getDay()] + ', ' + MONTHS[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
    var items = (map || itemsByDate())[selected] || [];

    var html = '<div class="cal-detail-title">' + escapeHtml(pretty) + '</div>';
    if (!items.length) {
      html += '<div class="cal-detail-empty">Nothing scheduled. Add an event below.</div>';
    } else {
      items.forEach(function(it) {
        if (it.kind === 'event') html += renderEvent(it.data);
        else                     html += renderTaskOverlay(it.data);
      });
    }
    /* add-event row */
    html +=
      '<div class="cal-add-row" data-date="' + selected + '">' +
        '<input type="time" class="time" id="cal-add-time" title="Optional time"/>' +
        '<input type="text" id="cal-add-title" placeholder="Add an event for this day…" autocomplete="off"/>' +
        '<button id="cal-add-btn" type="button" title="Add">+</button>' +
      '</div>';
    detailEl.innerHTML = html;

    /* wire add row */
    var addBtn   = document.getElementById('cal-add-btn');
    var addInp   = document.getElementById('cal-add-title');
    var addTime  = document.getElementById('cal-add-time');
    if (addBtn) {
      addBtn.addEventListener('click', function() {
        addEvent(selected, (addInp.value || '').trim(), addTime.value || null);
      });
    }
    if (addInp) {
      addInp.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          addEvent(selected, (addInp.value || '').trim(), addTime.value || null);
        }
      });
    }
  }

  function renderEvent(e) {
    if (editing === e.id) return renderEventEdit(e);
    var when = e.time ? e.time : 'all day';
    var meta = '';
    if (e.duration_min) meta += e.duration_min + 'min';
    if (e.notes) meta += (meta ? ' · ' : '') + (e.notes.length > 60 ? e.notes.slice(0, 60) + '…' : e.notes);
    return (
      '<div class="cal-evt" data-id="' + e.id + '" data-act="edit">' +
        '<div class="when' + (e.time ? '' : ' notime') + '">' + escapeHtml(when) + '</div>' +
        '<div class="body">' +
          '<div class="title">' + escapeHtml(e.title) + '</div>' +
          (meta ? '<div class="meta">' + escapeHtml(meta) + '</div>' : '') +
        '</div>' +
      '</div>'
    );
  }

  function renderEventEdit(e) {
    return (
      '<div class="cal-evt-edit" data-id="' + e.id + '">' +
        '<input type="text" class="ed-title" value="' + escapeHtml(e.title) + '" placeholder="Title"/>' +
        '<div class="cal-evt-edit-row">' +
          '<label>Date</label><input type="date" class="ed-date" value="' + escapeHtml(e.date) + '"/>' +
          '<label>Time</label><input type="time" class="ed-time" value="' + escapeHtml(e.time || '') + '"/>' +
          '<label>Mins</label><input type="number" class="ed-dur" min="0" max="1440" value="' + (e.duration_min || '') + '" style="width:60px;"/>' +
        '</div>' +
        '<textarea class="ed-notes" placeholder="Notes (optional)">' + escapeHtml(e.notes || '') + '</textarea>' +
        '<div class="cal-evt-edit-actions">' +
          '<button class="danger" data-act="delete">Delete</button>' +
          '<button class="cancel" data-act="cancel">Cancel</button>' +
          '<button class="save"   data-act="save">Save</button>' +
        '</div>' +
      '</div>'
    );
  }

  function renderTaskOverlay(t) {
    var cls = 'cal-evt is-task';
    var meta = 'task' + (t.assignee ? ' · @' + t.assignee : '');
    if (t.status === 'done') meta += ' · done';
    return (
      '<div class="' + cls + '" title="Open this task in the planner">' +
        '<div class="when notime">' + (t.status === 'done' ? '\u2713' : 'due') + '</div>' +
        '<div class="body">' +
          '<div class="title">' + escapeHtml(t.title) + '</div>' +
          '<div class="meta">' + escapeHtml(meta) + '</div>' +
        '</div>' +
      '</div>'
    );
  }

  /* ── event CRUD ───────────────────────────────────────────────────── */
  function addEvent(date, title, time) {
    if (!title) { setStatus('Title cannot be empty.', 'error'); return; }
    var body = { title: title, date: date };
    if (time) body.time = time;
    api('POST', '/api/calendar/event', body).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'add failed', 'error'); return; }
      events.push(res.body.event);
      setStatus('Added.', 'ok');
      render();
      refreshBadge();
    });
  }

  function saveEvent(id, row) {
    var patch = {
      title:        row.querySelector('.ed-title').value,
      date:         row.querySelector('.ed-date').value,
      time:         row.querySelector('.ed-time').value || null,
      duration_min: row.querySelector('.ed-dur').value || null,
      notes:        row.querySelector('.ed-notes').value,
    };
    api('PATCH', '/api/calendar/event/' + encodeURIComponent(id), patch).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'save failed', 'error'); return; }
      var idx = events.findIndex(function(e){ return e.id === id; });
      if (idx >= 0) events[idx] = res.body.event;
      editing = null;
      setStatus('Saved.', 'ok');
      render();
      refreshBadge();
    });
  }

  function deleteEvent(id) {
    if (!confirm('Delete this event?')) return;
    api('DELETE', '/api/calendar/event/' + encodeURIComponent(id)).then(function(res) {
      if (!res.ok) { setStatus(res.body.error || 'delete failed', 'error'); return; }
      events = events.filter(function(e){ return e.id !== id; });
      editing = null;
      setStatus('Deleted.', 'ok');
      render();
      refreshBadge();
    });
  }

  /* ── delegated click on grid + detail ─────────────────────────────── */
  gridEl.addEventListener('click', function(e) {
    var cell = e.target.closest('.cal-cell');
    if (!cell) return;
    var key = cell.getAttribute('data-date');
    if (!key) return;
    selected = key;
    editing  = null;
    try { localStorage.setItem(LS_SELECTED, selected); } catch(e){}
    /* if click landed on an "other-month" cell, jump to that month */
    var d = fromYmd(key);
    if (d.getMonth() !== view.month || d.getFullYear() !== view.year) {
      setMonth(d.getFullYear(), d.getMonth());
    } else {
      render();
    }
  });

  detailEl.addEventListener('click', function(e) {
    /* clicked an event row → enter edit mode */
    var ev = e.target.closest('.cal-evt[data-id]');
    if (ev) {
      var id = ev.getAttribute('data-id');
      var act = (e.target.closest('[data-act]') || ev).getAttribute('data-act');
      if (act === 'edit') {
        editing = id;
        render();
      }
      return;
    }
    /* clicked save/cancel/delete inside the inline editor */
    var editRow = e.target.closest('.cal-evt-edit[data-id]');
    if (!editRow) return;
    var id  = editRow.getAttribute('data-id');
    var btn = e.target.closest('[data-act]');
    if (!btn) return;
    var act = btn.getAttribute('data-act');
    if (act === 'save')   saveEvent(id, editRow);
    if (act === 'delete') deleteEvent(id);
    if (act === 'cancel') { editing = null; render(); }
  });

  /* ── keyboard ─────────────────────────────────────────────────────── */
  document.addEventListener('keydown', function(e) {
    if (panel.classList.contains('hidden')) return;
    var t = e.target;
    var typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (e.key === 'Escape') {
      if (editing) { editing = null; render(); return; }
      if (typing) return;  /* let inputs handle it */
      if (panel.contains(t)) setHidden(true);
    } else if (!typing && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      if (panel.contains(t) || t === document.body) {
        e.preventDefault();
        setMonth(view.year, view.month + (e.key === 'ArrowRight' ? 1 : -1));
      }
    }
  });

  /* ── boot + soft refresh while open ──────────────────────────────── */
  if (!panel.classList.contains('hidden')) load();
  /* poll every 60s while open so other browsers' edits show up */
  setInterval(function() {
    if (!panel.classList.contains('hidden') && !document.hidden) fetchData();
  }, 60000);
  /* badge always lit even if panel never opened */
  load();
})();
</script>
</body>
</html>
"""


@sock.route("/ws/tmux/<session>")
def ws_tmux(ws, session):
    """WebSocket → PTY bridge: attaches to tmux pane 1 of the given session."""
    target = f"{session}:main.1"
    pid, master_fd = pty.fork()

    if pid == 0:
        # Child: set TERM so tmux/clear work correctly inside xterm.js
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        os.execvpe("tmux", ["tmux", "attach-session", "-t", target], env)
        os._exit(1)

    # Set a reasonable terminal size (xterm.js default)
    def _resize(cols, rows):
        s = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, s)

    _resize(220, 50)

    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.02)
            if r:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                ws.send(data)

            try:
                msg = ws.receive(timeout=0)
            except Exception:
                msg = None

            if msg is not None:
                if isinstance(msg, str):
                    msg = msg.encode()
                if msg[:2] == b"\x01r":
                    # Resize message: "\x01r<cols>,<rows>"
                    try:
                        parts = msg[2:].decode().split(",")
                        _resize(int(parts[0]), int(parts[1]))
                    except Exception:
                        pass
                else:
                    os.write(master_fd, msg)
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        os.waitpid(pid, os.WNOHANG)


def _browser_roots():
    return {
        "docs":    JARVIS_ROOT / "docs",
        "apps":    JARVIS_ROOT / "apps",
        "modules": JARVIS_ROOT / "modules",
        "agents":  JARVIS_ROOT / "agents",
        "archive": ARCHIVE_DIR,
    }

# Sub-directories inside each agent folder to include in the agents browser.
# apps/ and logs/ are excluded (binary/noisy); everything else is fair game.
AGENT_BROWSE_DIRS = {"context.md", "docs", "utilities", "routines"}


@app.route("/api/browser/list")
def api_browser_list():
    """List files in docs/, apps/, modules/, or agents/ for the file browser panel."""
    section = request.args.get("section", "docs")
    roots   = _browser_roots()
    root    = roots.get(section)
    if not root or not root.exists():
        return jsonify({"files": []})

    files = []

    if section in ("agents", "archive"):
        # Walk <name>/{context.md, docs/**, utilities/**, routines/**}
        for agent_dir in sorted(root.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            name = agent_dir.name
            # context.md at root of agent
            ctx = agent_dir / "context.md"
            if ctx.is_file():
                files.append({"path": f"{name}/context.md", "size": ctx.stat().st_size})
            # sub-dirs
            for subdir_name in ("docs", "utilities", "routines"):
                subdir = agent_dir / subdir_name
                if not subdir.is_dir():
                    continue
                for p in sorted(subdir.rglob("*")):
                    if p.is_file() and not p.name.startswith("."):
                        rel = str(p.relative_to(root))
                        files.append({"path": rel, "size": p.stat().st_size})
    else:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                rel = str(p.relative_to(root))
                files.append({"path": rel, "size": p.stat().st_size})

    return jsonify({"files": files, "section": section})


@app.route("/api/browser/file", methods=["GET", "POST"])
def api_browser_file():
    """Return or save raw content of a file in docs/, apps/, modules/, or agents/."""
    section = request.args.get("section", "docs")
    path    = request.args.get("path", "")
    root    = _browser_roots().get(section)
    if not root:
        return jsonify({"error": "invalid section"}), 400
    full = (root / path).resolve()
    if not str(full).startswith(str(root.resolve())):
        return jsonify({"error": "path traversal denied"}), 403

    if request.method == "POST":
        if not full.exists() or not full.is_file():
            return jsonify({"error": "file not found"}), 404
        content = (request.json or {}).get("content")
        if content is None:
            return jsonify({"error": "missing content"}), 400
        try:
            full.write_text(content, encoding="utf-8")
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    if not full.exists() or not full.is_file():
        return jsonify({"error": "file not found"}), 404
    try:
        content = full.read_text(errors="replace")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    ext = full.suffix.lower()
    kind = "markdown" if ext in (".md",) else "code"
    return jsonify({"content": content, "kind": kind, "name": full.name, "path": path})


@app.route("/api/agent/master/<name>", methods=["POST"])
def api_agent_master(name: str):
    agent_dir = AGENTS_DIR / name
    if not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404
    value = bool((request.json or {}).get("master", False))
    _set_master(agent_dir, value)
    return jsonify({"ok": True, "master": value})


@app.route("/api/agent/tags/<name>", methods=["GET", "POST"])
def api_agent_tags(name: str):
    agent_dir = AGENTS_DIR / name
    if not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404
    if request.method == "POST":
        tags = (request.json or {}).get("tags", [])
        tags = [str(t).strip().lower() for t in tags if str(t).strip()]
        _write_tags(agent_dir, tags)
        return jsonify({"ok": True, "tags": sorted(set(tags))})
    return jsonify({"tags": _read_tags(agent_dir)})


@app.route("/api/agent/model/<name>", methods=["GET", "POST"])
def api_agent_model(name: str):
    """Read or write the cursor-agent model slug for one agent.

    Persistence is a single-line file `agents/<name>/.cursor-model`. The
    new model only takes effect after pane 1 is relaunched, so the
    frontend pairs a POST here with /api/stop + /api/start.
    """
    agent_dir = AGENTS_DIR / name
    if not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    if request.method == "POST":
        slug = ((request.json or {}).get("model") or "").strip()
        if slug not in _MODEL_SLUGS:
            return jsonify({
                "error":   "invalid model slug",
                "slug":    slug,
                "choices": [m["slug"] for m in MODEL_CHOICES],
            }), 400
        try:
            (agent_dir / ".cursor-model").write_text(slug + "\n")
        except Exception as e:
            return jsonify({"error": f"write failed: {e}"}), 500
        return jsonify({
            "ok":      True,
            "model":   slug,
            "default": DEFAULT_MODEL,
            "note":    "Restart agent for change to take effect.",
        })

    return jsonify({
        "model":   read_agent_model(agent_dir),
        "default": DEFAULT_MODEL,
        "choices": MODEL_CHOICES,
    })


@app.route("/api/task/delegate", methods=["POST"])
def api_task_delegate():
    """Drop a task into <name>'s RC channel.

    The agent's own rocketchat.py monitor will pick up the new message on
    its next poll and feed it to pane 1 via tmux send-keys, exactly like
    any other RC message. Side effects we want for free:
      - the dispatch shows up in agents/<name>/logs/dispatch.log
      - the dispatch counter on the tile ticks up
      - the message is auditable in RC (you can see what you asked for)

    Body: { "name": "<agent>", "text": "<task text>" }
    """
    body = request.json or {}
    name = (body.get("name") or "").strip()
    text = (body.get("text") or "").strip()
    if not name or not text:
        return jsonify({"error": "name and text required"}), 400
    agent_dir = AGENTS_DIR / name
    if not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    client = _get_rc_client()
    if client is None:
        return jsonify({
            "error": f"RC client unavailable: {_rc_client_err or 'unknown'}"
        }), 503

    channel = f"#{name}"
    try:
        client.send_message(channel, text)
    except Exception as e:
        return jsonify({"error": f"send failed: {e}"}), 502

    return jsonify({"ok": True, "channel": channel, "agent": name})


# ── Task Planner ────────────────────────────────────────────────────────
# Personal work/project tracker for the operator running JARVIS. State
# lives in data/planner.json (gitignored — your private todo list, not
# anything to do with agents). The agent fleet has its own dispatch UX
# via the Quick Task dialog; this is purely for the human-in-the-loop.
PLANNER_DIR   = JARVIS_ROOT / "data"
PLANNER_FILE  = PLANNER_DIR / "planner.json"
_planner_lock = _threading.Lock()

_PLANNER_DEFAULT_SETTINGS = {
    "digest_hour":       8,
    "digest_minute":     0,
    "digest_enabled":    True,
    "last_digest_date":  "",   # YYYY-MM-DD (server local) of last fire
}

_PLANNER_DEFAULT = {
    "version":  2,
    "settings": dict(_PLANNER_DEFAULT_SETTINGS),
    "projects": [
        {"id": "p-inbox", "name": "Inbox", "color": "#fbbf24", "order": 0},
    ],
    "staff":    [],   # [{username, name, added_at}]
    "tasks":    [],
    "dm_state": {},   # {<username>: {last_digest:{ts,rc_msg_id,task_ids:[]}, last_polled_ts}}
}


def _planner_load() -> dict:
    """Read state, healing missing/corrupt files. Always returns a usable dict.
    Auto-migrates v1 -> v2: adds settings/staff/dm_state, renames task.agent
    -> task.assignee one time."""
    if not PLANNER_FILE.is_file():
        return json.loads(json.dumps(_PLANNER_DEFAULT))
    try:
        doc = json.loads(PLANNER_FILE.read_text())
    except Exception:
        return json.loads(json.dumps(_PLANNER_DEFAULT))
    if not isinstance(doc, dict):
        doc = {}

    doc.setdefault("projects", [])
    doc.setdefault("tasks", [])
    doc.setdefault("staff", [])
    doc.setdefault("dm_state", {})

    # Settings: deep-default each key so old files pick up new fields safely.
    s = doc.get("settings") or {}
    if not isinstance(s, dict):
        s = {}
    for k, v in _PLANNER_DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    doc["settings"] = s

    # Inbox always exists (delete edge case + first-load).
    if not doc["projects"]:
        doc["projects"] = list(_PLANNER_DEFAULT["projects"])
    if not any(p.get("id") == "p-inbox" for p in doc["projects"]):
        doc["projects"].insert(0, dict(_PLANNER_DEFAULT["projects"][0]))

    # v1 -> v2 task field migration: agent -> assignee. Done in-place; saved
    # the next time the file is written. We keep agent as fallback until then.
    if int(doc.get("version", 1)) < 2:
        for t in doc["tasks"]:
            if "assignee" not in t:
                t["assignee"] = t.get("agent") or None
            t.pop("agent", None)
        doc["version"] = 2

    # New per-task field defaults (older tasks won't have them).
    for t in doc["tasks"]:
        t.setdefault("assignee", None)

    return doc


def _planner_save(doc: dict) -> None:
    """Atomic write: tmp file + replace. Cheap for sub-MB JSON."""
    PLANNER_DIR.mkdir(exist_ok=True)
    tmp = PLANNER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(PLANNER_FILE)


def _planner_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _planner_id(prefix: str) -> str:
    import secrets
    ms = int(time.time() * 1000)
    return f"{prefix}-{ms}-{secrets.token_hex(2)}"


@app.route("/api/planner/state")
def api_planner_state():
    """Full planner state — small enough to ship every load (typically <100 KB)."""
    with _planner_lock:
        return jsonify(_planner_load())


@app.route("/api/planner/task", methods=["POST"])
def api_planner_task_create():
    body  = request.json or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    with _planner_lock:
        doc = _planner_load()
        project = body.get("project") or "p-inbox"
        if not any(p["id"] == project for p in doc["projects"]):
            project = "p-inbox"
        try:
            prio = int(body.get("priority") or 0)
        except Exception:
            prio = 0
        prio = max(0, min(3, prio))
        status = body.get("status") if body.get("status") in ("todo", "doing", "done") else "todo"
        # Accept `assignee` (preferred) or legacy `agent` for back-compat
        # with any external callers / older clients.
        assignee = (body.get("assignee") or body.get("agent") or "").strip() or None
        task = {
            "id":           _planner_id("t"),
            "title":        title[:500],
            "notes":        (body.get("notes") or "").strip()[:4000],
            "project":      project,
            "status":       status,
            "priority":     prio,
            "due":          (body.get("due") or None),
            "assignee":     assignee,
            "created_at":   _planner_now(),
            "updated_at":   _planner_now(),
            "completed_at": _planner_now() if status == "done" else None,
        }
        doc["tasks"].append(task)
        _planner_save(doc)
    return jsonify({"ok": True, "task": task})


@app.route("/api/planner/task/<tid>", methods=["PATCH"])
def api_planner_task_update(tid):
    patch = request.json or {}
    with _planner_lock:
        doc  = _planner_load()
        task = next((t for t in doc["tasks"] if t["id"] == tid), None)
        if not task:
            return jsonify({"error": "task not found"}), 404
        if "title" in patch:
            v = (patch["title"] or "").strip()
            if v:
                task["title"] = v[:500]
        if "notes" in patch:
            task["notes"] = (patch["notes"] or "").strip()[:4000]
        if "project" in patch and any(p["id"] == patch["project"] for p in doc["projects"]):
            task["project"] = patch["project"]
        if "status" in patch and patch["status"] in ("todo", "doing", "done"):
            task["status"] = patch["status"]
        if "priority" in patch:
            try:
                task["priority"] = max(0, min(3, int(patch["priority"])))
            except Exception:
                pass
        if "due" in patch:
            task["due"] = patch["due"] or None
        # Accept either field name; canonical key is `assignee`.
        if "assignee" in patch:
            task["assignee"] = (patch["assignee"] or "").strip() or None
        elif "agent" in patch:
            task["assignee"] = (patch["agent"] or "").strip() or None
        task["updated_at"] = _planner_now()
        # keep completed_at synced to status
        if task["status"] == "done" and not task.get("completed_at"):
            task["completed_at"] = _planner_now()
        elif task["status"] != "done":
            task["completed_at"] = None
        _planner_save(doc)
    return jsonify({"ok": True, "task": task})


@app.route("/api/planner/task/<tid>", methods=["DELETE"])
def api_planner_task_delete(tid):
    with _planner_lock:
        doc    = _planner_load()
        before = len(doc["tasks"])
        doc["tasks"] = [t for t in doc["tasks"] if t["id"] != tid]
        if len(doc["tasks"]) == before:
            return jsonify({"error": "task not found"}), 404
        _planner_save(doc)
    return jsonify({"ok": True})


@app.route("/api/planner/project", methods=["POST"])
def api_planner_project_create():
    body  = request.json or {}
    name  = (body.get("name") or "").strip()
    color = (body.get("color") or "#fbbf24").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with _planner_lock:
        doc = _planner_load()
        if any(p["name"].lower() == name.lower() for p in doc["projects"]):
            return jsonify({"error": "project name already exists"}), 409
        proj = {
            "id":    _planner_id("p"),
            "name":  name[:80],
            "color": color[:24] or "#fbbf24",
            "order": len(doc["projects"]),
        }
        doc["projects"].append(proj)
        _planner_save(doc)
    return jsonify({"ok": True, "project": proj})


@app.route("/api/planner/project/<pid>", methods=["PATCH"])
def api_planner_project_update(pid):
    patch = request.json or {}
    with _planner_lock:
        doc  = _planner_load()
        proj = next((p for p in doc["projects"] if p["id"] == pid), None)
        if not proj:
            return jsonify({"error": "project not found"}), 404
        if "name" in patch:
            new_name = (patch["name"] or "").strip()[:80]
            if new_name and not any(
                p["name"].lower() == new_name.lower() and p["id"] != pid
                for p in doc["projects"]
            ):
                proj["name"] = new_name
        if "color" in patch:
            proj["color"] = (patch["color"] or "#fbbf24").strip()[:24] or "#fbbf24"
        if "order" in patch:
            try:
                proj["order"] = int(patch["order"])
            except Exception:
                pass
        _planner_save(doc)
    return jsonify({"ok": True, "project": proj})


@app.route("/api/planner/project/<pid>", methods=["DELETE"])
def api_planner_project_delete(pid):
    """Delete a project. Tasks reassign to Inbox; pass ?delete_tasks=1 to nuke them.
    Inbox itself is protected."""
    if pid == "p-inbox":
        return jsonify({"error": "Inbox cannot be deleted"}), 400
    delete_tasks = request.args.get("delete_tasks") == "1"
    with _planner_lock:
        doc  = _planner_load()
        proj = next((p for p in doc["projects"] if p["id"] == pid), None)
        if not proj:
            return jsonify({"error": "project not found"}), 404
        doc["projects"] = [p for p in doc["projects"] if p["id"] != pid]
        if delete_tasks:
            doc["tasks"] = [t for t in doc["tasks"] if t.get("project") != pid]
        else:
            for t in doc["tasks"]:
                if t.get("project") == pid:
                    t["project"] = "p-inbox"
                    t["updated_at"] = _planner_now()
        _planner_save(doc)
    return jsonify({"ok": True})


# ── Planner: RC integration (staff, digest, replies) ────────────────────
PLANNER_DIGEST_LOG = PLANNER_DIR / "planner-digest.log"
_planner_user_cache: dict = {"ts": 0.0, "users": []}  # 60s memo of users.list
_PLANNER_USER_TTL = 60.0


def _planner_audit(event: str, **fields) -> None:
    """Append a JSONL audit line. Never raises — this is best-effort logging."""
    try:
        PLANNER_DIR.mkdir(exist_ok=True)
        rec = {"ts": _planner_now(), "event": event}
        rec.update(fields)
        with open(PLANNER_DIGEST_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _planner_workload(doc: dict) -> dict:
    """Map of assignee -> open-task count. Includes 'Unassigned' bucket."""
    out: dict = {}
    for t in doc["tasks"]:
        if t.get("status") == "done":
            continue
        key = (t.get("assignee") or "").strip() or ""
        out[key] = out.get(key, 0) + 1
    return out


def _planner_is_agent(name: str) -> bool:
    """An assignee is an `agent` if it matches a folder under agents/."""
    if not name:
        return False
    p = (AGENTS_DIR / name).resolve()
    try:
        return str(p).startswith(str(AGENTS_DIR.resolve())) and p.is_dir()
    except Exception:
        return False


def _planner_staff_list_users(force: bool = False) -> list[dict]:
    """Cached wrapper around RocketChat.list_users for the autocomplete."""
    global _planner_user_cache
    now = time.time()
    if not force and (now - _planner_user_cache["ts"]) < _PLANNER_USER_TTL:
        return _planner_user_cache["users"]
    client = _get_rc_client()
    if client is None:
        return []
    try:
        users = client.list_users(count=500) or []
    except Exception as e:
        print(f"[planner] list_users failed: {e}")
        return _planner_user_cache["users"]
    # Normalize: keep only fields we actually use, drop bots/inactive.
    norm = []
    for u in users:
        username = (u.get("username") or "").strip()
        if not username:
            continue
        if u.get("active") is False:
            continue
        norm.append({
            "username": username,
            "name":     (u.get("name") or u.get("username") or "").strip(),
            "_id":      u.get("_id") or "",
        })
    norm.sort(key=lambda x: x["username"].lower())
    _planner_user_cache = {"ts": now, "users": norm}
    return norm


@app.route("/api/planner/users/search")
def api_planner_users_search():
    """Autocomplete proxy: `q` filters cached `users.list` against username + name."""
    q = (request.args.get("q") or "").strip().lower()
    users = _planner_staff_list_users()
    if not users:
        client_err = _rc_client_err or "RC client not available"
        return jsonify({"users": [], "warning": client_err})
    if q:
        users = [
            u for u in users
            if q in u["username"].lower() or q in (u.get("name") or "").lower()
        ]
    return jsonify({"users": users[:25]})


@app.route("/api/planner/staff")
def api_planner_staff_list():
    """Staff list, each enriched with current open-task count (sorted desc)."""
    with _planner_lock:
        doc = _planner_load()
        wl  = _planner_workload(doc)
        out = []
        for s in doc["staff"]:
            out.append({**s, "open": wl.get(s["username"], 0)})
        out.sort(key=lambda s: (-s["open"], s["username"].lower()))
    return jsonify({
        "staff":    out,
        "settings": doc["settings"],
    })


@app.route("/api/planner/staff", methods=["POST"])
def api_planner_staff_add():
    body     = request.json or {}
    username = (body.get("username") or "").strip().lstrip("@")
    if not username:
        return jsonify({"error": "username required"}), 400
    # Resolve display name from RC (best-effort; falls back to username).
    name = username
    client = _get_rc_client()
    if client is not None:
        try:
            info = client.get_user_info(username) or {}
            user = info.get("user") or {}
            name = (user.get("name") or user.get("username") or username).strip()
        except Exception as e:
            return jsonify({"error": f"user not found in Rocket.Chat: {e}"}), 404
    with _planner_lock:
        doc = _planner_load()
        if any(s["username"].lower() == username.lower() for s in doc["staff"]):
            return jsonify({"error": "already in staff list"}), 409
        entry = {
            "username": username,
            "name":     name,
            "added_at": _planner_now(),
        }
        doc["staff"].append(entry)
        _planner_save(doc)
    return jsonify({"ok": True, "staff": entry})


@app.route("/api/planner/staff/<username>", methods=["DELETE"])
def api_planner_staff_remove(username):
    """Remove from staff. Tasks assigned to them are NOT deleted."""
    with _planner_lock:
        doc    = _planner_load()
        before = len(doc["staff"])
        doc["staff"] = [s for s in doc["staff"] if s["username"].lower() != username.lower()]
        if len(doc["staff"]) == before:
            return jsonify({"error": "not in staff list"}), 404
        _planner_save(doc)
    return jsonify({"ok": True})


@app.route("/api/planner/settings")
def api_planner_settings_get():
    with _planner_lock:
        doc = _planner_load()
    s = dict(doc["settings"])
    # Surface the loop's "next fire" so the UI can show a countdown.
    s["next_digest_at"] = _planner_next_digest_iso(doc["settings"])
    s["server_now"]     = datetime.now().isoformat(timespec="seconds")
    return jsonify(s)


@app.route("/api/planner/settings", methods=["PATCH"])
def api_planner_settings_patch():
    patch = request.json or {}
    with _planner_lock:
        doc = _planner_load()
        s   = doc["settings"]
        if "digest_hour" in patch:
            try:
                s["digest_hour"] = max(0, min(23, int(patch["digest_hour"])))
            except Exception:
                pass
        if "digest_minute" in patch:
            try:
                s["digest_minute"] = max(0, min(59, int(patch["digest_minute"])))
            except Exception:
                pass
        if "digest_enabled" in patch:
            s["digest_enabled"] = bool(patch["digest_enabled"])
        # Manually clearing last_digest_date forces a re-fire today.
        if patch.get("reset_today"):
            s["last_digest_date"] = ""
        _planner_save(doc)
    return jsonify({"ok": True, "settings": doc["settings"]})


def _planner_next_digest_iso(settings: dict) -> str:
    """ISO-8601 (server local, no tz) for the next time the digest will fire."""
    if not settings.get("digest_enabled"):
        return ""
    now = datetime.now()
    target = now.replace(hour=int(settings.get("digest_hour", 8)),
                         minute=int(settings.get("digest_minute", 0)),
                         second=0, microsecond=0)
    today_str = now.strftime("%Y-%m-%d")
    last_sent = settings.get("last_digest_date") or ""
    # If today's slot is still in the future AND we haven't sent today, use today.
    if target > now and last_sent != today_str:
        return target.isoformat(timespec="seconds")
    # Otherwise, next fire is the same time tomorrow.
    from datetime import timedelta as _td
    return (target + _td(days=1)).isoformat(timespec="seconds")


# ── Digest body builder + sender ────────────────────────────────────────
def _planner_format_due(iso_date: str) -> str:
    """'Fri' / 'today' / 'tomorrow' / 'Apr 30' — small human-friendly hint."""
    if not iso_date:
        return ""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except Exception:
        return iso_date
    today = datetime.now().date()
    delta = (d - today).days
    if delta == 0:  return "today"
    if delta == 1:  return "tomorrow"
    if delta == -1: return "yesterday"
    if 1 < delta < 7:
        return d.strftime("%a")
    return d.strftime("%b ") + str(d.day)


def _planner_user_tasks(doc: dict, username: str) -> list[dict]:
    """Open (todo+doing) tasks assigned to <username>, sorted overdue→due→prio."""
    today = datetime.now().date().isoformat()
    rows = []
    for t in doc["tasks"]:
        if t.get("status") == "done":
            continue
        if (t.get("assignee") or "").lower() != username.lower():
            continue
        rows.append(t)
    def _key(t):
        due       = t.get("due") or "9999-12-31"
        is_overdue = 0 if (due < today) else 1
        return (is_overdue, due, -int(t.get("priority") or 0))
    rows.sort(key=_key)
    return rows


def _planner_build_digest_body(display_name: str, tasks: list[dict]) -> str:
    prio_lbl = {0: "", 1: "low", 2: "med", 3: "high"}
    today = datetime.now().date().isoformat()
    lines = [
        f"Good morning, {display_name} — {len(tasks)} task(s) for today:",
        "",
    ]
    for i, t in enumerate(tasks, 1):
        bits = []
        if t.get("priority"):
            bits.append(prio_lbl[t["priority"]])
        if t.get("due"):
            label = _planner_format_due(t["due"])
            if t["due"] < today:
                bits.append(f"OVERDUE {label}")
            else:
                bits.append(f"due {label}")
        meta = f"  [{', '.join(bits)}]" if bits else ""
        lines.append(f"{i}. {t['title']}{meta}  ·  `{t['id']}`")
    lines += [
        "",
        "Reply `done <#>` to mark one done (e.g. `done 2`),",
        "or `done all` if you cleared everything.",
    ]
    return "\n".join(lines)


def _planner_send_one_digest(doc: dict, username: str) -> tuple[bool, str]:
    """Send today's digest to one assignee. Mutates doc.dm_state on success.
    Returns (sent_bool, status_message)."""
    tasks = _planner_user_tasks(doc, username)
    if not tasks:
        _planner_audit("digest_skip", assignee=username, reason="no_tasks")
        return False, "no open tasks"

    is_agent = _planner_is_agent(username)
    if is_agent:
        # Agents read their channel via tmux monitor; route there. Reply
        # tracking won't work for agents (their replies go to Cursor), but
        # the daily reminder is still useful so the agent context is fresh.
        target  = f"#{username}"
        display = username
    else:
        # Look up display name from staff list (fall back to username).
        staff = next((s for s in doc["staff"]
                      if s["username"].lower() == username.lower()), None)
        display = (staff or {}).get("name") or username
        target  = username

    body = _planner_build_digest_body(display, tasks)
    client = _get_rc_client()
    if client is None:
        _planner_audit("digest_skip", assignee=username, reason="no_rc_client")
        return False, "RC client unavailable"

    try:
        if is_agent:
            res = client.send_message(target, body)
        else:
            res = client.send_direct(target, body)
    except Exception as e:
        _planner_audit("digest_skip", assignee=username, reason=f"send_failed:{e}")
        return False, f"send failed: {e}"

    msg     = (res or {}).get("message") or {}
    msg_id  = msg.get("_id") or ""
    room_id = msg.get("rid") or ""
    ts      = _planner_now()
    state   = doc["dm_state"].setdefault(username, {})
    state["last_digest"] = {
        "ts":         ts,
        "rc_msg_id":  msg_id,
        "rc_room_id": room_id,
        "task_ids":   [t["id"] for t in tasks],
        "kind":       "agent" if is_agent else "user",
    }
    state.setdefault("last_polled_ts", ts)
    _planner_audit("digest_sent",
                   assignee=username, kind=state["last_digest"]["kind"],
                   task_count=len(tasks), task_ids=state["last_digest"]["task_ids"],
                   rc_msg_id=msg_id, rc_room_id=room_id)
    return True, "sent"


@app.route("/api/planner/digest/send_now", methods=["POST"])
def api_planner_digest_send_now():
    """Manually fire a digest to one assignee (staff or agent). Useful for the
    'Send now' button in the staff panel and for testing."""
    assignee = (request.args.get("assignee") or "").strip()
    if not assignee:
        body = request.json or {}
        assignee = (body.get("assignee") or "").strip()
    if not assignee:
        return jsonify({"error": "assignee required"}), 400
    with _planner_lock:
        doc = _planner_load()
        ok, msg = _planner_send_one_digest(doc, assignee)
        if ok:
            _planner_save(doc)
    if not ok:
        return jsonify({"error": msg}), 400 if msg == "no open tasks" else 502
    _planner_audit("manual_send", assignee=assignee)
    return jsonify({"ok": True, "status": msg})


# ── Reply parser: 'done 1' / 'done t-...' / 'done all' ──────────────────
import re as _re  # noqa: E402

_PLANNER_DONE_RE = _re.compile(r"^\s*done\b\s*(.*?)\s*$", _re.IGNORECASE)


def _planner_parse_done(text: str, last_digest: dict, open_task_ids: set) -> list[str]:
    """Return list of task_ids to mark done. Empty list = no match."""
    if not text or not last_digest:
        return []
    m = _PLANNER_DONE_RE.match(text)
    if not m:
        return []
    arg = (m.group(1) or "").strip()
    digest_ids = list(last_digest.get("task_ids") or [])
    if not arg:
        # Bare 'done' — ambiguous unless there's exactly ONE open task in
        # the most recent digest. Guards against accidental "done."
        remaining = [tid for tid in digest_ids if tid in open_task_ids]
        if len(remaining) == 1:
            return remaining
        return []
    if arg.lower() == "all":
        return [tid for tid in digest_ids if tid in open_task_ids]
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(digest_ids):
            tid = digest_ids[idx]
            return [tid] if tid in open_task_ids else []
        return []
    # Treat as a task-id (full or prefix). Restrict to ids that were
    # actually in this user's digest — no cross-user marking.
    arg_l = arg.lower().lstrip("`").rstrip("`")
    matches = [tid for tid in digest_ids
               if tid in open_task_ids and tid.lower().startswith(arg_l)]
    return matches[:1]


def _planner_poll_replies(doc: dict) -> int:
    """Scan each user's DM history since their last_polled_ts; mark replies.
    Returns number of tasks newly marked done. Mutates `doc` in place; caller
    is responsible for save()ing if anything changed."""
    client = _get_rc_client()
    if client is None:
        return 0

    bot_uid_attr = getattr(client, "_uid", "") or ""
    open_task_ids = {t["id"] for t in doc["tasks"] if t.get("status") != "done"}
    marked_total  = 0

    for username, state in list(doc["dm_state"].items()):
        last_digest = state.get("last_digest") or {}
        digest_ts   = last_digest.get("ts") or ""
        if not digest_ts:
            continue
        # Skip agents — their replies go to Cursor pane, not back to the bot.
        if last_digest.get("kind") == "agent":
            continue
        # Skip stale digests (>48h) to bound API usage.
        try:
            sent_dt = datetime.fromisoformat(digest_ts.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - sent_dt).total_seconds() > 48 * 3600:
                continue
        except Exception:
            continue

        room_id = last_digest.get("rc_room_id") or ""
        if not room_id:
            try:
                room_id = (client.open_direct(username) or {}).get("room", {}).get("_id", "")
                last_digest["rc_room_id"] = room_id
            except Exception:
                continue
        if not room_id:
            continue

        try:
            msgs = client.get_direct_messages(room_id, count=20) or []
        except Exception as e:
            _planner_audit("poll_error", assignee=username, error=str(e))
            continue

        cutoff = state.get("last_polled_ts") or digest_ts
        newest_seen = cutoff
        for m in msgs:
            mts = m.get("ts") or ""
            if not mts or mts <= cutoff:
                continue
            sender_uid = (m.get("u") or {}).get("_id") or ""
            sender_un  = (m.get("u") or {}).get("username") or ""
            # Skip bot-authored messages.
            if bot_uid_attr and sender_uid == bot_uid_attr:
                if mts > newest_seen: newest_seen = mts
                continue
            if sender_un and sender_un.lower() != username.lower():
                if mts > newest_seen: newest_seen = mts
                continue
            text = (m.get("msg") or "").strip()
            ids  = _planner_parse_done(text, last_digest, open_task_ids)
            if ids:
                for tid in ids:
                    task = next((t for t in doc["tasks"] if t["id"] == tid), None)
                    if not task or task.get("status") == "done":
                        continue
                    task["status"]       = "done"
                    task["completed_at"] = _planner_now()
                    task["updated_at"]   = _planner_now()
                    open_task_ids.discard(tid)
                    marked_total += 1
                    _planner_audit("reply_done",
                                   assignee=username, task_id=tid,
                                   reply_msg_id=m.get("_id"), text=text[:200])
            else:
                _planner_audit("reply_ignored",
                               assignee=username, text=text[:200],
                               reply_msg_id=m.get("_id"))
            if mts > newest_seen:
                newest_seen = mts
        state["last_polled_ts"] = newest_seen

    return marked_total


# ── Daemon loop: daily digest + reply polling ──────────────────────────
def _planner_loop() -> None:
    """Single background tick (60s). Fires daily digest + polls replies."""
    print("  [planner] daemon thread started")
    while True:
        try:
            with _planner_lock:
                doc      = _planner_load()
                changed  = False
                settings = doc["settings"]
                # Digest fire window (1-minute resolution).
                if settings.get("digest_enabled"):
                    now       = datetime.now()
                    today_str = now.strftime("%Y-%m-%d")
                    hh        = int(settings.get("digest_hour", 8))
                    mm        = int(settings.get("digest_minute", 0))
                    last_date = settings.get("last_digest_date") or ""
                    if (now.hour == hh and now.minute == mm
                            and last_date != today_str):
                        # Build the bucket of all assignees with ≥1 open task.
                        wl = _planner_workload(doc)
                        recipients = [
                            u for u, n in wl.items()
                            if u and n > 0
                        ]
                        sent = 0
                        for user in recipients:
                            ok, _ = _planner_send_one_digest(doc, user)
                            if ok:
                                sent += 1
                        settings["last_digest_date"] = today_str
                        changed = True
                        _planner_audit("digest_cycle_fired",
                                       sent=sent, recipients=len(recipients),
                                       hour=hh, minute=mm)
                # Reply polling (every tick).
                marked = _planner_poll_replies(doc)
                if marked > 0:
                    changed = True
                if changed:
                    _planner_save(doc)
        except Exception as e:
            print(f"  [planner] loop error: {e}")
        time.sleep(60)


def _planner_start_loop() -> None:
    """Start the daemon thread once. App.run uses `use_reloader=False` so
    we don't have to worry about Werkzeug spawning a parent watcher; this
    guard catches the rare case where reloader gets re-enabled later."""
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return  # reloader's parent watcher — child will start it
    t = _threading.Thread(target=_planner_loop, name="planner-loop", daemon=True)
    t.start()


_planner_start_loop()


# ── Calendar ────────────────────────────────────────────────────────────
# Personal monthly schedule for the operator. Events live in
# data/calendar.json (gitignored). Planner tasks with `due` dates are
# overlaid by the frontend, so we don't duplicate that data here.
CALENDAR_FILE  = PLANNER_DIR / "calendar.json"
_calendar_lock = _threading.Lock()

_CALENDAR_DEFAULT = {"version": 1, "events": []}


def _calendar_load() -> dict:
    """Read state, healing missing/corrupt files."""
    if not CALENDAR_FILE.is_file():
        return json.loads(json.dumps(_CALENDAR_DEFAULT))
    try:
        doc = json.loads(CALENDAR_FILE.read_text())
    except Exception:
        return json.loads(json.dumps(_CALENDAR_DEFAULT))
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("version", 1)
    doc.setdefault("events", [])
    return doc


def _calendar_save(doc: dict) -> None:
    PLANNER_DIR.mkdir(exist_ok=True)
    tmp = CALENDAR_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(CALENDAR_FILE)


_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = _re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_event_payload(body: dict) -> tuple[dict, str]:
    """Coerce + validate a partial event dict. Returns (cleaned, err_msg)."""
    out: dict = {}
    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            return ({}, "title required")
        out["title"] = title[:200]
    if "date" in body:
        d = (body.get("date") or "").strip()
        if not _DATE_RE.match(d):
            return ({}, "date must be YYYY-MM-DD")
        out["date"] = d
    if "time" in body:
        t = body.get("time")
        if t in (None, ""):
            out["time"] = None
        elif _TIME_RE.match(str(t)):
            out["time"] = str(t)
        else:
            return ({}, "time must be HH:MM (24h) or empty")
    if "duration_min" in body:
        v = body.get("duration_min")
        if v in (None, ""):
            out["duration_min"] = None
        else:
            try:
                out["duration_min"] = max(0, min(60 * 24, int(v)))
            except Exception:
                return ({}, "duration_min must be an integer (minutes)")
    if "notes" in body:
        out["notes"] = (body.get("notes") or "").strip()[:2000]
    if "color" in body:
        c = (body.get("color") or "").strip()
        out["color"] = c[:24] if c else None
    return (out, "")


@app.route("/api/calendar/events")
def api_calendar_events():
    """Return events. Optional `from`/`to` filter (inclusive, YYYY-MM-DD)."""
    q_from = (request.args.get("from") or "").strip()
    q_to   = (request.args.get("to")   or "").strip()
    with _calendar_lock:
        doc = _calendar_load()
    events = doc["events"]
    if q_from and _DATE_RE.match(q_from):
        events = [e for e in events if (e.get("date") or "") >= q_from]
    if q_to and _DATE_RE.match(q_to):
        events = [e for e in events if (e.get("date") or "") <= q_to]
    # sort by date, then time (untimed first)
    events = sorted(events, key=lambda e: (
        e.get("date") or "",
        e.get("time") or "",
    ))
    return jsonify({"events": events})


@app.route("/api/calendar/event", methods=["POST"])
def api_calendar_event_create():
    body = request.json or {}
    if "title" not in body or "date" not in body:
        return jsonify({"error": "title and date required"}), 400
    fields, err = _validate_event_payload(body)
    if err:
        return jsonify({"error": err}), 400
    with _calendar_lock:
        doc = _calendar_load()
        ev = {
            "id":           _planner_id("e"),
            "title":        fields["title"],
            "date":         fields["date"],
            "time":         fields.get("time"),
            "duration_min": fields.get("duration_min"),
            "notes":        fields.get("notes", ""),
            "color":        fields.get("color"),
            "created_at":   _planner_now(),
            "updated_at":   _planner_now(),
        }
        doc["events"].append(ev)
        _calendar_save(doc)
    return jsonify({"ok": True, "event": ev})


@app.route("/api/calendar/event/<eid>", methods=["PATCH"])
def api_calendar_event_update(eid):
    body = request.json or {}
    fields, err = _validate_event_payload(body)
    if err:
        return jsonify({"error": err}), 400
    with _calendar_lock:
        doc = _calendar_load()
        ev  = next((e for e in doc["events"] if e["id"] == eid), None)
        if not ev:
            return jsonify({"error": "event not found"}), 404
        for k, v in fields.items():
            ev[k] = v
        ev["updated_at"] = _planner_now()
        _calendar_save(doc)
    return jsonify({"ok": True, "event": ev})


@app.route("/api/calendar/event/<eid>", methods=["DELETE"])
def api_calendar_event_delete(eid):
    with _calendar_lock:
        doc    = _calendar_load()
        before = len(doc["events"])
        doc["events"] = [e for e in doc["events"] if e["id"] != eid]
        if len(doc["events"]) == before:
            return jsonify({"error": "event not found"}), 404
        _calendar_save(doc)
    return jsonify({"ok": True})


@app.route("/api/agent/files/list")
def api_agent_files_list():
    """List all files in an agent's directory (recursive, excludes apps/)."""
    name = request.args.get("agent", "").strip()
    if not name:
        return jsonify({"error": "agent required"}), 400
    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    files = []
    skip = {"apps"}  # exclude injected app files with secrets
    for p in sorted(agent_dir.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(agent_dir).parts
        if parts[0] in skip or p.name.startswith("."):
            continue
        rel  = str(p.relative_to(agent_dir))
        size = p.stat().st_size
        files.append({"name": p.name, "path": rel, "size": size})
    return jsonify({"files": files})


@app.route("/api/agent/files/upload", methods=["POST"])
def api_agent_files_upload():
    """Upload one or more files into the agent's uploads/ directory."""
    from werkzeug.utils import secure_filename
    name = request.args.get("agent", "").strip()
    if not name:
        return jsonify({"error": "agent required"}), 400
    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    upload_dir = agent_dir / "uploads"
    upload_dir.mkdir(exist_ok=True)

    saved = []
    for f in request.files.getlist("files"):
        fname = secure_filename(f.filename or "upload")
        dest  = upload_dir / fname
        f.save(dest)
        saved.append(fname)
    return jsonify({"ok": True, "files": saved})


@app.route("/api/agent/files/download")
def api_agent_files_download():
    """Download or inline-view a file from an agent's directory."""
    from flask import send_file
    name    = request.args.get("agent", "").strip()
    relpath = request.args.get("path", "").strip()
    inline  = request.args.get("inline", "0") == "1"
    if not name or not relpath:
        return jsonify({"error": "agent and path required"}), 400
    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404
    full = (agent_dir / relpath).resolve()
    if not str(full).startswith(str(agent_dir)) or not full.is_file():
        return jsonify({"error": "file not found"}), 404
    return send_file(full, as_attachment=not inline, download_name=full.name)


# ── Agent logs (local) ────────────────────────────────────────────────────────
LOG_TAIL_MAX_BYTES = 256 * 1024   # don't read more than 256 KB from the tail


def _agent_log_files(agent_dir: Path) -> list[Path]:
    """Return all .log files in the agent's logs/ dir + top-level .log files."""
    out: list[Path] = []
    logs_dir = agent_dir / "logs"
    if logs_dir.is_dir():
        out.extend(sorted(p for p in logs_dir.iterdir() if p.is_file() and p.suffix == ".log"))
    # top-level .log files (some agents/apps drop them at the agent root)
    for p in sorted(agent_dir.iterdir()):
        if p.is_file() and p.suffix == ".log":
            out.append(p)
    return out


@app.route("/api/agent/logs/list")
def api_agent_logs_list():
    name = request.args.get("agent", "").strip()
    if not name:
        return jsonify({"error": "agent required"}), 400
    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    items = []
    for p in _agent_log_files(agent_dir):
        try:
            st = p.stat()
            items.append({
                "name":  p.name,
                "rel":   str(p.relative_to(agent_dir)),
                "size":  st.st_size,
                "mtime": int(st.st_mtime),
            })
        except OSError:
            continue
    return jsonify({"logs": items})


@app.route("/api/agent/logs/tail")
def api_agent_logs_tail():
    """Tail the last N lines of an agent log file.

    Query params:
        agent=<name>                required
        file=<relative-path>        required (e.g. logs/monitor.log)
        lines=<int>                 default 300, max 5000
    """
    name = request.args.get("agent", "").strip()
    rel  = request.args.get("file", "").strip()
    try:
        lines = max(10, min(5000, int(request.args.get("lines", "300"))))
    except ValueError:
        lines = 300
    if not name or not rel:
        return jsonify({"error": "agent and file required"}), 400

    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    full = (agent_dir / rel).resolve()
    if not str(full).startswith(str(agent_dir)) or not full.is_file() or full.suffix != ".log":
        return jsonify({"error": "log file not found"}), 404

    size = full.stat().st_size
    # Read up to LOG_TAIL_MAX_BYTES from end, then keep last N lines
    read_bytes = min(size, LOG_TAIL_MAX_BYTES)
    truncated_head = read_bytes < size
    try:
        with open(full, "rb") as fh:
            fh.seek(size - read_bytes)
            chunk = fh.read(read_bytes)
    except OSError as e:
        return jsonify({"error": f"read failed: {e}"}), 500

    text = chunk.decode("utf-8", errors="replace")
    # If we truncated head, drop the (likely partial) first line
    if truncated_head:
        idx = text.find("\n")
        if idx >= 0:
            text = text[idx + 1:]

    all_lines = text.splitlines()
    tailed = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return jsonify({
        "ok":             True,
        "name":           full.name,
        "rel":            rel,
        "size":           size,
        "returned_lines": len(tailed),
        "total_in_chunk": len(all_lines),
        "truncated_head": truncated_head or len(all_lines) > lines,
        "lines":          tailed,
    })


# ── Git remote helpers ────────────────────────────────────────────────────────
# Each agent maps to an SSH host alias (== agent name). If the remote host has
# a git repo somewhere in $HOME, we autodetect its location and cache the
# absolute path in agents/<name>/.git-remote-path so subsequent calls are fast.

GIT_PATH_FILE = ".git-remote-path"
SSH_TIMEOUT_FAST = 10   # status/log calls
SSH_TIMEOUT_SLOW = 60   # push (network upload)

# Common candidate paths to probe on the remote when autodetecting.
GIT_PROBE_PATHS = [
    "~/public_html",
    "~/htdocs",
    "~/www",
    "~/site",
    "~/repo",
    "~",
]


def _git_path_cache(agent_dir: Path) -> Path:
    return agent_dir / GIT_PATH_FILE


def _read_git_path(agent_dir: Path) -> str:
    f = _git_path_cache(agent_dir)
    if not f.is_file():
        return ""
    try:
        return f.read_text().strip()
    except Exception:
        return ""


def _write_git_path(agent_dir: Path, path: str) -> None:
    _git_path_cache(agent_dir).write_text(path.strip() + "\n")


def _ssh_run(host: str, remote_cmd: str, timeout: int = SSH_TIMEOUT_FAST):
    """Run a single command over SSH. Returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=5",
                    "-o", "StrictHostKeyChecking=accept-new",
                    host, remote_cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"SSH timeout after {timeout}s"
    except Exception as e:
        return 255, "", f"SSH error: {e}"


def _git_autodetect(host: str, agent_name: str) -> tuple[str, str]:
    """Probe common paths on the remote, return (path, error).

    On success: returns (toplevel_abs_path, ""). On failure: returns ("", reason).
    """
    # Build list of candidates: standard ones + ~/<agent_name>
    candidates = list(GIT_PROBE_PATHS)
    extra = f"~/{agent_name}"
    if extra not in candidates:
        candidates.insert(2, extra)

    # Single SSH command — loop through candidates, print first toplevel found.
    parts = []
    for c in candidates:
        # Use shell-quoted single string; cd then git rev-parse
        parts.append(f'( cd {c} 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null )')
    remote_cmd = " || ".join(parts) + " || true"

    rc, out, err = _ssh_run(host, remote_cmd)
    out = out.strip()
    if not out:
        msg = err.strip() or f"no .git found in any of: {', '.join(candidates)}"
        return "", msg
    # Take first non-empty line
    first = out.splitlines()[0].strip()
    return first, ""


def _shell_q(s: str) -> str:
    """Single-quote-safe quoting for embedding in shell over SSH."""
    if "'" not in s:
        return f"'{s}'"
    return "'" + s.replace("'", "'\"'\"'") + "'"


@app.route("/api/agent/git/status")
def api_agent_git_status():
    """Return git status for the agent's remote repo.

    Query params:
        agent=<name>           required
        rescan=1               force re-detect even if cache exists
    """
    name = request.args.get("agent", "").strip()
    rescan = request.args.get("rescan", "0") == "1"
    if not name:
        return jsonify({"error": "agent required"}), 400
    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    host = name  # SSH alias == agent name in this setup
    path = "" if rescan else _read_git_path(agent_dir)
    detected = False
    if not path:
        path, derr = _git_autodetect(host, name)
        if not path:
            return jsonify({
                "ok": False,
                "host": host,
                "path": "",
                "error": derr,
                "hint": "Set up a git repo on the remote first, then click Re-detect.",
            })
        _write_git_path(agent_dir, path)
        detected = True

    # Compose one SSH call that emits structured chunks separated by markers.
    cmd = (
        f"cd {_shell_q(path)} && "
        f"echo '<<BRANCH>>' && git rev-parse --abbrev-ref HEAD && "
        f"echo '<<REMOTE>>' && (git remote get-url origin 2>/dev/null || echo '') && "
        f"echo '<<UPSTREAM>>' && (git rev-parse --abbrev-ref --symbolic-full-name '@{{u}}' 2>/dev/null || echo '') && "
        f"echo '<<STATUS>>' && git status --porcelain && "
        f"echo '<<LOG>>' && git log -10 --pretty=format:'%h%x09%an%x09%ar%x09%s'"
    )
    rc, out, err = _ssh_run(host, cmd)
    if rc != 0:
        return jsonify({
            "ok": False,
            "host": host,
            "path": path,
            "error": err.strip() or out.strip() or f"git command failed (rc={rc})",
        })

    sections = {"BRANCH": "", "REMOTE": "", "UPSTREAM": "", "STATUS": "", "LOG": ""}
    cur = None
    for line in out.splitlines():
        m = line.strip()
        if m.startswith("<<") and m.endswith(">>"):
            cur = m[2:-2]
            continue
        if cur and cur in sections:
            sections[cur] += line + "\n"

    branch  = sections["BRANCH"].strip()
    remote  = sections["REMOTE"].strip()
    upstream = sections["UPSTREAM"].strip()
    changes = [l for l in sections["STATUS"].splitlines() if l.strip()]
    commits = []
    for line in sections["LOG"].splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        if len(parts) == 4:
            sha, author, when, subj = parts
            commits.append({"sha": sha, "author": author, "when": when, "subject": subj})

    return jsonify({
        "ok": True,
        "host": host,
        "path": path,
        "branch": branch,
        "remote": remote,
        "upstream": upstream,
        "dirty": bool(changes),
        "changes": changes,
        "commits": commits,
        "detected_now": detected,
    })


@app.route("/api/agent/git/push", methods=["POST"])
def api_agent_git_push():
    """git add -A && git commit -m "<msg>" && git push  on the remote."""
    body = request.json or {}
    name = (request.args.get("agent") or body.get("agent") or "").strip()
    msg  = (body.get("message") or "").strip()
    if not name:
        return jsonify({"error": "agent required"}), 400
    if not msg:
        return jsonify({"error": "commit message required"}), 400
    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    path = _read_git_path(agent_dir)
    if not path:
        return jsonify({"error": "no detected git path; click Re-detect first"}), 400

    host = name
    # Use git -c user.* to ensure commit succeeds even if remote git lacks identity
    cmd = (
        f"cd {_shell_q(path)} && "
        f"git -c user.name='JARVIS Dashboard' -c user.email='jarvis@{host}' "
        f"-c commit.gpgsign=false add -A && "
        f"if git diff --cached --quiet; then echo 'NO_CHANGES'; exit 0; fi && "
        f"git -c user.name='JARVIS Dashboard' -c user.email='jarvis@{host}' "
        f"-c commit.gpgsign=false commit -m {_shell_q(msg)} && "
        f"git push 2>&1"
    )
    rc, out, err = _ssh_run(host, cmd, timeout=SSH_TIMEOUT_SLOW)
    output = (out + ("\n" + err if err.strip() else "")).strip()
    no_changes = "NO_CHANGES" in out and rc == 0
    return jsonify({
        "ok": rc == 0,
        "rc": rc,
        "output": output,
        "no_changes": no_changes,
    })


@app.route("/api/agent/git/detect", methods=["POST"])
def api_agent_git_detect():
    """Re-detect the remote git path or set it manually.

    Body: {path: "/abs/path"} to set manually, or {} to auto-detect.
    """
    body = request.json or {}
    name = (request.args.get("agent") or body.get("agent") or "").strip()
    manual = (body.get("path") or "").strip()
    if not name:
        return jsonify({"error": "agent required"}), 400
    agent_dir = (AGENTS_DIR / name).resolve()
    if not str(agent_dir).startswith(str(AGENTS_DIR.resolve())) or not agent_dir.is_dir():
        return jsonify({"error": "agent not found"}), 404

    host = name
    if manual:
        # Verify manual path is actually a git repo
        rc, out, err = _ssh_run(host, f"cd {_shell_q(manual)} && git rev-parse --show-toplevel")
        if rc != 0:
            return jsonify({"ok": False, "error": (err or out or "not a git repo").strip()})
        toplevel = out.strip().splitlines()[0]
        _write_git_path(agent_dir, toplevel)
        return jsonify({"ok": True, "path": toplevel})

    path, derr = _git_autodetect(host, name)
    if not path:
        # Also clear the cache on failed re-detect
        try:
            _git_path_cache(agent_dir).unlink()
        except Exception:
            pass
        return jsonify({"ok": False, "error": derr})
    _write_git_path(agent_dir, path)
    return jsonify({"ok": True, "path": path})


@app.route("/api/agent/archive", methods=["POST"])
def api_agent_archive():
    """Move agent dir to archive/, kill its tmux session. Files are preserved."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    agent_dir = AGENTS_DIR / name
    if not agent_dir.is_dir():
        return jsonify({"error": f"Agent '{name}' not found"}), 404

    # Kill tmux session
    session = name.replace(".", "-")
    subprocess.run(["tmux", "kill-session", "-t", session],
                   capture_output=True)

    # Move to archive/<name> (add timestamp suffix if already archived)
    import shutil as _shutil
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dest = ARCHIVE_DIR / name
    if dest.exists():
        from datetime import datetime as _dt
        dest = ARCHIVE_DIR / f"{name}_{_dt.now().strftime('%Y%m%d_%H%M%S')}"
    _shutil.move(str(agent_dir), str(dest))
    return jsonify({"ok": True, "message": f"Archived {name} → archive/{dest.name}"})


@app.route("/api/agent/restore", methods=["POST"])
def api_agent_restore():
    """Move agent dir from archive/ back to agents/."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    src = ARCHIVE_DIR / name
    if not src.is_dir():
        return jsonify({"error": f"Archived agent '{name}' not found"}), 404

    # Strip any timestamp suffix to get the clean agent name
    base_name = name.split("_")[0] if "_20" in name else name
    dest = AGENTS_DIR / base_name
    if dest.exists():
        return jsonify({"error": f"agents/{base_name} already exists — rename or remove it first"}), 409

    import shutil as _shutil
    _shutil.move(str(src), str(dest))
    return jsonify({"ok": True, "message": f"Restored {name} → agents/{base_name}"})


# ── v2 -> v4 migration ───────────────────────────────────────────────────────

@app.route("/api/migrate/v2/list")
def api_migrate_v2_list():
    """Return migratable v2 agents discovered on local disk."""
    try:
        agents = migrate_v2.discover_v2_agents()
    except Exception as e:
        return jsonify({"error": f"discovery failed: {e}", "agents": []}), 500
    return jsonify({"v2_root": str(migrate_v2.V2_ROOT), "agents": agents})


@app.route("/api/migrate/v2/preview", methods=["POST"])
def api_migrate_v2_preview():
    """Build (but don't execute) a migration plan."""
    body = request.json or {}
    v2 = (body.get("v2_name") or "").strip()
    v4 = (body.get("v4_name") or "").strip() or migrate_v2.suggest_v4_name(v2)
    opts = body.get("options") or {}
    if not v2:
        return jsonify({"error": "v2_name required"}), 400
    plan = migrate_v2.build_plan(v2, v4, opts)
    return jsonify(plan)


@app.route("/api/migrate/v2/run", methods=["POST"])
def api_migrate_v2_run():
    """Execute a migration, streaming progress lines as SSE."""
    body = request.json or {}
    v2 = (body.get("v2_name") or "").strip()
    v4 = (body.get("v4_name") or "").strip() or migrate_v2.suggest_v4_name(v2)
    opts = body.get("options") or {}
    if not v2:
        return jsonify({"error": "v2_name required"}), 400

    plan = migrate_v2.build_plan(v2, v4, opts)
    if not plan["ok"]:
        # Return errors immediately as SSE events so the UI can show them
        def err_stream():
            for e in plan["errors"]:
                yield f"data: {json.dumps('FAIL: ' + e)}\n\n"
            yield f"data: {json.dumps({'__exit__': 1})}\n\n"
        return Response(err_stream(), mimetype="text/event-stream",
                        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    def generate():
        try:
            for line in migrate_v2.execute_plan(plan):
                yield f"data: {json.dumps(line)}\n\n"
            yield f"data: {json.dumps({'__exit__': 0, 'v4_name': plan['v4_name']})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps('FATAL: ' + str(e))}\n\n"
            yield f"data: {json.dumps({'__exit__': 1})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    """Run deploy.py for a new or existing agent, stream output as SSE."""
    body = request.json or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    cmd = ["python3", str(DEPLOY_PY), name, "--no-attach"]
    if body.get("no_channel"):   cmd.append("--no-channel")
    if body.get("no_webhook"):   cmd.append("--no-webhook")
    if body.get("interval"):     cmd += ["--interval", str(int(body["interval"]))]
    if body.get("ssh_host") and body["ssh_host"] != name:
        pass  # ssh_host is inferred from name; kept for future use
    if body.get("mailinbox_host"):     cmd += ["--mailinbox-host",     body["mailinbox_host"]]
    if body.get("mailinbox_email"):    cmd += ["--mailinbox-email",    body["mailinbox_email"]]
    if body.get("mailinbox_password"): cmd += ["--mailinbox-password", body["mailinbox_password"]]

    def generate():
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(JARVIS_ROOT)
        )
        for line in proc.stdout:
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        proc.wait()
        yield f"data: {json.dumps({'__exit__': proc.returncode})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ── Agent Auto-Hibernate ────────────────────────────────────────────────
# Idle agents (no dispatch.log activity for `idle_hours`) get their tmux
# session killed to free RAM. The dashboard keeps polling Rocket.Chat for
# them; a fresh inbound message wakes them back up automatically (post a
# short ack to the channel, then redeploy). Default-on for every agent;
# opt-out per agent via `always_on`. State is centralised in a single
# JSON file so a Flask restart doesn't lose context.
HIBERNATION_FILE   = PLANNER_DIR / "hibernation.json"
HIBERNATION_LOG    = PLANNER_DIR / "hibernation.log"
_hibernation_lock  = _threading.RLock()  # reentrant: _hib_last_inbound -> _hib_load_cached re-enters from inside hot routes
_hib_room_id_cache: dict = {}            # agent name -> rid
_hib_load_cache: dict    = {"ts": 0.0, "doc": None}
_HIB_LOAD_TTL = 5.0

_HIB_DEFAULT_SETTINGS = {
    "enabled":          True,
    "idle_hours":       24.0,
    "poll_interval_sec": 60,
    "ack_message":      "Waking up, one sec...",
    "ready_message":    "✓ Ready — what can I help with?",
    "exclude_tags":     ["always-on"],
    "post_wake_grace_min": 10,            # min minutes before a fresh wake can re-hibernate
}

_HIB_DEFAULT_AGENT = {
    "always_on":         False,           # never sleep (skip loop)
    "disabled":          False,           # force off — sleep on next tick, never wake
    "status":            "running",       # running | hibernated | waking
    "hibernated_at":     None,
    "waking_started":    None,
    "wake_completed_at": None,            # used as activity floor (see _hib_last_inbound)
    "last_seen_sub_ts":  None,            # subscription cursor
    "last_inbound_ts":   None,            # convenience for tooltip
    "wake_count_today":  0,
    "wake_count_date":   "",              # YYYY-MM-DD bucket
}

_HIB_DEFAULT = {
    "version":  1,
    "settings": dict(_HIB_DEFAULT_SETTINGS),
    "agents":   {},
}


def _hib_load() -> dict:
    """Read hibernation state, healing missing/corrupt files."""
    if not HIBERNATION_FILE.is_file():
        return json.loads(json.dumps(_HIB_DEFAULT))
    try:
        doc = json.loads(HIBERNATION_FILE.read_text())
    except Exception:
        return json.loads(json.dumps(_HIB_DEFAULT))
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("version", 1)
    s = doc.get("settings") or {}
    if not isinstance(s, dict):
        s = {}
    for k, v in _HIB_DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    doc["settings"] = s
    a = doc.get("agents") or {}
    if not isinstance(a, dict):
        a = {}
    doc["agents"] = a
    return doc


def _hib_save(doc: dict) -> None:
    """Atomic write: tmp file + replace."""
    PLANNER_DIR.mkdir(exist_ok=True)
    tmp = HIBERNATION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    tmp.replace(HIBERNATION_FILE)
    _hib_load_cache["doc"] = None  # invalidate cache


def _hib_load_cached() -> dict:
    """Cheap read for hot paths (monitor_heartbeat). 5s TTL."""
    now = time.time()
    cached = _hib_load_cache.get("doc")
    if cached is not None and now - _hib_load_cache["ts"] < _HIB_LOAD_TTL:
        return cached
    with _hibernation_lock:
        doc = _hib_load()
    _hib_load_cache["doc"] = doc
    _hib_load_cache["ts"]  = now
    return doc


def _hib_audit(event: str, **fields) -> None:
    """Append a JSONL audit line. Best-effort; never raises."""
    try:
        PLANNER_DIR.mkdir(exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "event": event}
        rec.update(fields)
        with open(HIBERNATION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _hib_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hib_ensure_agent(doc: dict, name: str) -> dict:
    """Get-or-create per-agent state record."""
    a = doc["agents"].get(name)
    if not isinstance(a, dict):
        a = {}
    for k, v in _HIB_DEFAULT_AGENT.items():
        a.setdefault(k, v)
    doc["agents"][name] = a
    return a


def _hib_parse_dispatch_tail(name: str) -> datetime | None:
    """Tail dispatch.log (last ~32 KB) and return the final `"ts"` field as a
    datetime. Falls back to monitor.log mtime, then None."""
    log_path = AGENTS_DIR / name / "logs" / "dispatch.log"
    if log_path.is_file():
        try:
            size = log_path.stat().st_size
            with open(log_path, "rb") as f:
                if size > 32_768:
                    f.seek(-32_768, 2)
                tail = f.read().decode("utf-8", errors="replace")
            import re as _re_local
            matches = _re_local.findall(r'"ts"\s*:\s*"([^"]+)"', tail)
            if matches:
                ts_raw = matches[-1]
                try:
                    if ts_raw.endswith("Z"):
                        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    return datetime.fromisoformat(ts_raw)
                except Exception:
                    pass
        except Exception:
            pass
    mon = AGENTS_DIR / name / "logs" / "monitor.log"
    if mon.is_file():
        try:
            return datetime.fromtimestamp(mon.stat().st_mtime, tz=timezone.utc)
        except Exception:
            pass
    return None


def _hib_last_inbound(name: str, doc: dict | None = None) -> datetime | None:
    """Last activity timestamp for an agent, used to decide if it's idle.

    Returns the LATER of:
      - The last `"ts"` in agents/<name>/logs/dispatch.log (or monitor.log mtime fallback)
      - The agent's `wake_completed_at` (treated as fresh activity)

    Why include wake time? When the daemon wakes a hibernated agent, the new
    monitor needs ~30s to start polling and write its first dispatch.log
    entry. Without this floor, the very next loop tick (60s later) would see
    the *old* dispatch.log timestamp (often days/weeks ago) and immediately
    re-hibernate the agent before it has a chance to do any work. Treating
    the wake itself as activity gives the agent breathing room equal to the
    full `idle_hours` window before it becomes eligible to sleep again."""
    log_ts = _hib_parse_dispatch_tail(name)

    wake_ts = None
    if doc is None:
        try:
            doc = _hib_load_cached()
        except Exception:
            doc = None
    if doc:
        a = (doc.get("agents") or {}).get(name) or {}
        ws = a.get("wake_completed_at")
        if ws:
            try:
                wake_ts = datetime.fromisoformat(ws)
            except Exception:
                wake_ts = None

    candidates = [t for t in (log_ts, wake_ts) if t is not None]
    return max(candidates) if candidates else None


def _hib_resolve_room(name: str) -> str:
    """Cache `#<name>` → roomId via RC. Empty string on failure (never raises)."""
    cached = _hib_room_id_cache.get(name)
    if cached:
        return cached
    client = _get_rc_client()
    if not client:
        return ""
    try:
        rid = client.get_room_id(f"#{name}")
        if rid:
            _hib_room_id_cache[name] = rid
        return rid or ""
    except Exception:
        return ""


def _hib_should_skip(name: str, doc: dict, agent_tags: list) -> bool:
    """Skip the auto loop entirely for this agent?"""
    a = _hib_ensure_agent(doc, name)
    if a.get("always_on"):
        return True
    excl = set(doc["settings"].get("exclude_tags") or [])
    if excl and any(t in excl for t in (agent_tags or [])):
        return True
    return False


def _hib_idle_minutes(name: str) -> int:
    """Minutes since last dispatch.log activity. -1 when unknown."""
    last = _hib_last_inbound(name)
    if not last:
        return -1
    delta = datetime.now(timezone.utc) - last
    return max(0, int(delta.total_seconds() / 60))


# ── Hibernate / wake actions ────────────────────────────────────────────
def _hib_do_hibernate(name: str, manual: bool = False) -> tuple[bool, str]:
    """Snapshot RC cursor → kill tmux → mark hibernated. Returns (ok, msg)."""
    with _hibernation_lock:
        doc = _hib_load()
        a   = _hib_ensure_agent(doc, name)
        # Cache room id + freshest sub timestamp so the wake check has a
        # baseline. We do NOT need a full subscriptions.get here — the
        # daemon loop already has that pre-computed; manual hibernates pay
        # one extra HTTP call.
        rid = _hib_resolve_room(name)
        client = _get_rc_client()
        sub_ts = ""
        if client:
            try:
                for sub in client.list_subscriptions():
                    if sub.get("name") == name or sub.get("fname") == name:
                        sub_ts = sub.get("_updatedAt") or ""
                        break
            except Exception:
                pass
        a["last_seen_sub_ts"] = sub_ts or _hib_now_iso()
        a["status"]           = "hibernated"
        a["hibernated_at"]    = _hib_now_iso()
        a["waking_started"]   = None
        last_in = _hib_last_inbound(name)
        a["last_inbound_ts"]  = last_in.isoformat(timespec="seconds") if last_in else None
        _hib_audit("hibernate_initiated",
                   agent=name, manual=manual, sub_cursor=sub_ts)
        _hib_save(doc)

    ok, out = _kill_tmux_session(name)
    _hib_audit("hibernate_complete",
               agent=name, manual=manual, kill_ok=ok, kill_output=out[:200])
    return ok, out


def _hib_do_wake(name: str, manual: bool = False, ack: bool = True) -> tuple[bool, str]:
    """Post ack message → mark waking → run deploy → mark running → post ready.

    Manual wakes also clear the `disabled` flag (escape hatch — clicking
    "Wake now" on a disabled agent is the user explicitly turning it back
    on; without this clear, the next loop tick would just put it right
    back to sleep)."""
    with _hibernation_lock:
        doc = _hib_load()
        a   = _hib_ensure_agent(doc, name)
        if manual and a.get("disabled"):
            a["disabled"] = False
            _hib_audit("agent_re_enabled_via_wake", agent=name)
        a["status"]          = "waking"
        a["waking_started"]  = _hib_now_iso()
        # Bump wake counter (per local day).
        today = datetime.now().strftime("%Y-%m-%d")
        if a.get("wake_count_date") != today:
            a["wake_count_date"]  = today
            a["wake_count_today"] = 0
        a["wake_count_today"] = int(a.get("wake_count_today", 0)) + 1
        ack_text   = (doc["settings"].get("ack_message")   or "").strip()
        ready_text = (doc["settings"].get("ready_message") or "").strip()
        _hib_audit("wake_triggered",
                   agent=name, manual=manual, ack_enabled=ack)
        _hib_save(doc)

    # Best-effort ack message (non-fatal).
    if ack and ack_text:
        client = _get_rc_client()
        if client:
            try:
                client.send_message(f"#{name}", ack_text)
            except Exception as e:
                _hib_audit("wake_ack_failed", agent=name, error=str(e))

    ok, stdout, stderr = _run_deploy(name)
    with _hibernation_lock:
        doc = _hib_load()
        a   = _hib_ensure_agent(doc, name)
        if ok:
            a["status"]            = "running"
            a["hibernated_at"]     = None
            a["waking_started"]    = None
            # Crucial: this floor prevents the very next loop tick from
            # re-hibernating the agent based on the stale dispatch.log
            # timestamp (the new monitor needs ~30s to write its first
            # entry). See _hib_last_inbound() for the read side.
            a["wake_completed_at"] = _hib_now_iso()
            _hib_audit("wake_complete", agent=name, manual=manual)
        else:
            # Leave status='waking' so the UI shows the in-flight failure
            # state; the next loop tick (or a manual retry) can recover.
            _hib_audit("wake_failed", agent=name, manual=manual,
                       stderr=(stderr or "")[:400])
        _hib_save(doc)

    # Best-effort "ready" follow-up so the user knows we're back on the
    # case (the actual cursor-agent reply may take another 10-30s as the
    # monitor polls and dispatches the original message).
    if ok and ack and ready_text:
        client = _get_rc_client()
        if client:
            try:
                client.send_message(f"#{name}", ready_text)
                _hib_audit("wake_ready_sent", agent=name)
            except Exception as e:
                _hib_audit("wake_ready_failed", agent=name, error=str(e))

    return ok, stdout if ok else (stderr or stdout)


# ── Daemon loop: per-minute idle/wake decisions ─────────────────────────
def _hibernation_loop() -> None:
    """One subscriptions.get per tick (regardless of agent count); then per-agent
    idle/wake decisions. Failures in any single agent never stop the loop."""
    print("  [hibernate] daemon thread started")
    while True:
        try:
            settings = _hib_load_cached()["settings"]
            interval = max(15, int(settings.get("poll_interval_sec") or 60))
        except Exception:
            interval = 60

        try:
            with _hibernation_lock:
                doc = _hib_load()
            settings = doc["settings"]
            if not settings.get("enabled"):
                time.sleep(interval)
                continue

            idle_secs = max(60.0, float(settings.get("idle_hours") or 24) * 3600.0)

            # Single subscriptions.get per tick — channel-count-agnostic.
            sub_ts_by_name: dict[str, str] = {}
            client = _get_rc_client()
            if client:
                try:
                    for sub in client.list_subscriptions():
                        nm = sub.get("name") or sub.get("fname")
                        if nm:
                            sub_ts_by_name[nm] = sub.get("_updatedAt") or ""
                except Exception as e:
                    _hib_audit("poll_error", error=str(e))

            now = datetime.now(timezone.utc)
            changed = False
            agents_on_disk = [d.name for d in AGENTS_DIR.iterdir()
                              if d.is_dir() and not d.name.startswith(".")]
            for name in agents_on_disk:
                try:
                    tags = _read_tags(AGENTS_DIR / name)
                    if _hib_should_skip(name, doc, tags):
                        continue
                    a = _hib_ensure_agent(doc, name)
                    status = a.get("status") or "running"

                    # Disabled agents: stay off. If they're somehow running
                    # (manual /api/start, post-deploy, etc.), force-sleep on
                    # this tick. If hibernated, skip wake check entirely.
                    if a.get("disabled"):
                        if status == "running":
                            _hib_save(doc)
                            _hib_do_hibernate(name, manual=False)
                            with _hibernation_lock:
                                doc = _hib_load()
                        continue

                    if status == "running":
                        last = _hib_last_inbound(name, doc)
                        if last is None:
                            continue  # no log file yet — let it warm up
                        idle = (now - last).total_seconds()
                        # Extra guard: never sleep an agent within
                        # post_wake_grace_min minutes of its last wake (belt
                        # and suspenders alongside the wake_completed_at
                        # floor in _hib_last_inbound — covers exotic clock
                        # skew between the dashboard and dispatch.log).
                        grace_min = int(settings.get("post_wake_grace_min") or 10)
                        wake_iso  = a.get("wake_completed_at")
                        if wake_iso:
                            try:
                                wake_dt = datetime.fromisoformat(wake_iso)
                                if (now - wake_dt).total_seconds() < grace_min * 60:
                                    continue
                            except Exception:
                                pass
                        if idle >= idle_secs:
                            _hib_save(doc)  # flush before slow op
                            _hib_do_hibernate(name, manual=False)
                            with _hibernation_lock:
                                doc = _hib_load()
                            changed = False  # already saved inside helper
                    elif status == "hibernated":
                        cur_sub = sub_ts_by_name.get(name) or ""
                        cached  = a.get("last_seen_sub_ts") or ""
                        if cur_sub and cur_sub > cached:
                            _hib_save(doc)
                            # Wake in a worker thread so the loop tick stays
                            # snappy (deploy can take 30s+).
                            _threading.Thread(
                                target=_hib_do_wake,
                                args=(name,),
                                kwargs={"manual": False, "ack": True},
                                daemon=True,
                                name=f"hib-wake-{name}",
                            ).start()
                            with _hibernation_lock:
                                doc = _hib_load()
                            changed = False
                except Exception as e:
                    _hib_audit("agent_tick_error", agent=name, error=str(e))

            if changed:
                with _hibernation_lock:
                    _hib_save(doc)
        except Exception as e:
            print(f"  [hibernate] loop error: {e}")

        time.sleep(interval)


def _hibernation_start_loop() -> None:
    """Start the daemon thread once. App.run uses use_reloader=False so we
    don't have to worry about Werkzeug double-spawn."""
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    t = _threading.Thread(target=_hibernation_loop, name="hibernation-loop", daemon=True)
    t.start()


_hibernation_start_loop()


# ── API routes ──────────────────────────────────────────────────────────
@app.route("/api/agent/hibernation")
def api_hibernation_state():
    """Full state + per-agent idle_minutes/online flags. Used to render UI."""
    with _hibernation_lock:
        doc = _hib_load()
        # Make sure every on-disk agent has an entry so the UI doesn't have
        # to defend against missing keys.
        for d in AGENTS_DIR.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                _hib_ensure_agent(doc, d.name)
        # Compute live diagnostics (don't persist).
        sessions = get_sessions()
        out_agents = {}
        for name, a in doc["agents"].items():
            entry = dict(a)
            entry["idle_minutes"] = _hib_idle_minutes(name)
            entry["session_alive"] = session_for(name) in sessions
            out_agents[name] = entry
        # Hibernated count (handy for the global widget).
        hib_count = sum(1 for a in out_agents.values()
                        if a.get("status") == "hibernated")
        return jsonify({
            "version":  doc.get("version", 1),
            "settings": doc["settings"],
            "agents":   out_agents,
            "hibernated_count": hib_count,
        })


@app.route("/api/agent/hibernation/settings", methods=["PATCH"])
def api_hibernation_settings_patch():
    body = request.json or {}
    with _hibernation_lock:
        doc = _hib_load()
        s   = doc["settings"]
        if "enabled" in body:
            s["enabled"] = bool(body["enabled"])
        if "idle_hours" in body:
            try:
                s["idle_hours"] = max(0.05, float(body["idle_hours"]))
            except (TypeError, ValueError):
                return jsonify({"error": "idle_hours must be a number"}), 400
        if "poll_interval_sec" in body:
            try:
                s["poll_interval_sec"] = max(15, int(body["poll_interval_sec"]))
            except (TypeError, ValueError):
                return jsonify({"error": "poll_interval_sec must be int"}), 400
        if "ack_message" in body:
            s["ack_message"] = str(body["ack_message"])[:500]
        if "ready_message" in body:
            s["ready_message"] = str(body["ready_message"])[:500]
        if "post_wake_grace_min" in body:
            try:
                s["post_wake_grace_min"] = max(0, int(body["post_wake_grace_min"]))
            except (TypeError, ValueError):
                return jsonify({"error": "post_wake_grace_min must be int"}), 400
        if "exclude_tags" in body:
            tags = body["exclude_tags"]
            if not isinstance(tags, list):
                return jsonify({"error": "exclude_tags must be list"}), 400
            s["exclude_tags"] = [str(t).strip() for t in tags if str(t).strip()]
        _hib_save(doc)
        _hib_audit("settings_updated", **{k: s[k] for k in s})
        return jsonify({"ok": True, "settings": s})


@app.route("/api/agent/hibernation/<name>", methods=["PATCH"])
def api_hibernation_agent_patch(name: str):
    """Update per-agent flags (`always_on`, `disabled`, or `mode`).

    `mode` is the preferred 3-way switch — accepts "auto" | "always_on" |
    "disabled" and writes both flags atomically with mutual exclusion.
    Setting `always_on` and `disabled` directly still works but the caller
    is responsible for not setting both true at once."""
    if not (AGENTS_DIR / name).is_dir():
        return jsonify({"error": "agent not found"}), 404
    body = request.json or {}
    with _hibernation_lock:
        doc = _hib_load()
        a   = _hib_ensure_agent(doc, name)

        if "mode" in body:
            mode = (body.get("mode") or "auto").lower()
            if mode not in ("auto", "always_on", "disabled"):
                return jsonify({"error": "mode must be auto|always_on|disabled"}), 400
            a["always_on"] = (mode == "always_on")
            a["disabled"]  = (mode == "disabled")
        else:
            if "always_on" in body:
                a["always_on"] = bool(body["always_on"])
                if a["always_on"]:
                    a["disabled"] = False
            if "disabled" in body:
                a["disabled"] = bool(body["disabled"])
                if a["disabled"]:
                    a["always_on"] = False

        _hib_save(doc)
        _hib_audit("agent_updated", agent=name,
                   always_on=a.get("always_on"),
                   disabled=a.get("disabled"))
        return jsonify({"ok": True, "agent": a})


@app.route("/api/agent/hibernation/<name>/hibernate", methods=["POST"])
def api_hibernation_manual_hibernate(name: str):
    """Force-sleep an agent now (used by the UI button)."""
    if not (AGENTS_DIR / name).is_dir():
        return jsonify({"error": "agent not found"}), 404
    _hib_audit("manual_hibernate", agent=name)
    ok, out = _hib_do_hibernate(name, manual=True)
    return jsonify({"ok": ok, "output": out})


@app.route("/api/agent/hibernation/<name>/wake", methods=["POST"])
def api_hibernation_manual_wake(name: str):
    """Force-wake an agent now (UI button). Skips the ack message — manual
    wake usually means the operator is already in front of the channel."""
    if not (AGENTS_DIR / name).is_dir():
        return jsonify({"error": "agent not found"}), 404
    _hib_audit("manual_wake", agent=name)
    ok, out = _hib_do_wake(name, manual=True, ack=False)
    return jsonify({"ok": ok, "output": out})


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML,
                                  now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                  rocketchat_url=rocketchat_base_url())


if __name__ == "__main__":
    print("\n  JARVIS v4 Dashboard — http://localhost:5112\n")
    app.run(host="0.0.0.0", port=5112, debug=True, use_reloader=False, threaded=True)
