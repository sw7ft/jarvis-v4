#!/usr/bin/env python3
"""
deploy.py — Jarvis v4 agent deployer.

    python3 deploy.py <agent.name> [options]

What it does (see MASTER-CONTEXT.md for the full spec):

  1. Scaffolds  agents/<agent.name>/{context.md, apps/}
  2. Copies     apps/master-rocketchat.py -> agents/<agent.name>/apps/rocketchat.py
                with DEFAULT_* config injected at the top.
  3. Ensures    RocketChat channel #<agent.name> exists.
  4. Registers  an incoming webhook for that channel; writes its URL into the
                per-agent rocketchat.py copy.
  5. Kills      any existing tmux session named <session_name> (dot -> dash).
  6. Launches   tmux session with one window 'main' and TWO panes (1 and 2).
                  Pane 1: cursor agent "read context.md"
                  Pane 2: python3 apps/rocketchat.py monitor #<agent.name> ...
  7. Attaches   to the session.

Re-running for an existing agent re-scaffolds the per-agent rocketchat.py copy
(re-using the existing webhook if present) and recreates the tmux session.

Options:
  --interval N              Monitor poll interval (seconds, default 10)
  --system-prompt S         Persona/system prompt for monitor + injected default
  --webhook-name N          Webhook display name (default: '<agent.name> webhook')
  --no-webhook              Skip webhook registration (still reuses an existing one)
  --no-channel              Skip channel auto-create
  --no-attach               Don't `tmux attach` at the end (just leave session detached)
  --dry-run                 Print actions without executing
  --mailinbox-host H        Mail-in-a-Box hostname (e.g. mail.example.com)
  --mailinbox-email E       Agent's email address on the mail server
  --mailinbox-password P    IMAP/SMTP password for the agent's mailbox
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT              = Path(__file__).resolve().parent
APPS_DIR          = ROOT / "apps"
TEMPLATES_DIR     = ROOT / "templates"
AGENTS_DIR        = ROOT / "agents"
MASTER_RC         = APPS_DIR / "master-rocketchat.py"
MASTER_MAILINBOX  = APPS_DIR / "mailinbox.py"
MASTER_BROWSER    = APPS_DIR / "browser.py"
CONTEXT_TPL       = TEMPLATES_DIR / "agent-context.md"
UTILITIES_TPL     = TEMPLATES_DIR / "utilities" / "README.md"
ROUTINES_TPL      = TEMPLATES_DIR / "routines" / "README.md"
SANDBOX_RULES_TPL = TEMPLATES_DIR / "sandbox.mdc"
SANDBOX_JSON_TPL  = TEMPLATES_DIR / "sandbox.json"

DEFAULT_PERSONA = (
    "You are a JARVIS v4 agent. Read context.md before acting. "
    "ALWAYS reply in the RocketChat channel — NEVER send a DM. "
    "After composing your answer, send it with: "
    "python3 apps/rocketchat.py send \"#<channel>\" \"your actual reply text\" "
    "from your agent dir (channel is in context.md Identity table). "
    "Never send placeholder or template text. Stay in scope."
)

# ─── Cursor agent model selection ──────────────────────────────────────────
# Per-agent override lives at agents/<name>/.cursor-model (single line, the
# slug). Absent file → DEFAULT_MODEL is used. Slug list: `cursor agent
# --list-models`.
DEFAULT_MODEL = "composer-2.5"


def read_agent_model(agent_dir: Path) -> str:
    """Return the model slug for an agent (override file or DEFAULT_MODEL)."""
    f = agent_dir / ".cursor-model"
    if f.is_file():
        val = f.read_text().strip()
        if val:
            return val
    return DEFAULT_MODEL


# ─── tiny logger ────────────────────────────────────────────────────────────

def info(msg: str): print(f"  \033[36m▸\033[0m {msg}")
def ok(msg: str):   print(f"  \033[32m✓\033[0m {msg}")
def warn(msg: str): print(f"  \033[33m⚠\033[0m {msg}")
def err(msg: str):  print(f"  \033[31m✗\033[0m {msg}", file=sys.stderr)


# ─── name validation / session derivation ───────────────────────────────────

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        sys.exit(f"Invalid agent name: {name!r}. Allowed: alphanum, '.', '-', '_' (must start alphanum).")
    return name

def session_for(name: str) -> str:
    # tmux's session:window.pane target syntax treats '.' as a separator.
    return name.replace(".", "-")


# ─── shell helpers ──────────────────────────────────────────────────────────

def run(cmd: list[str], *, check: bool = True, capture: bool = False, dry: bool = False):
    if dry:
        info(f"[dry-run] {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


# ─── RocketChat helpers (lazy import; not needed for --dry-run) ─────────────

def _rc_client():
    sys.path.insert(0, str(APPS_DIR))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("master_rocketchat", MASTER_RC)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
    except Exception as e:
        sys.exit(f"Failed to import master-rocketchat.py: {e}")
    try:
        return mod.RocketChat.from_config(), mod
    except FileNotFoundError:
        sys.exit("No RocketChat config. Run: python3 apps/master-rocketchat.py setup")
    except Exception as e:
        sys.exit(f"RocketChat login failed: {e}")


def ensure_channel(rc, name: str, dry: bool):
    """Ensure a private group #<name> exists with the admin + bot users.

    Convention: every agent room is a PRIVATE group containing exactly two
    members — the admin (e.g. 'matt') and the bot (e.g. 'Jarvis').
    """
    if dry:
        info(f"[dry-run] ensure private group #{name} with admin + bot members")
        return

    # Read who admin/bot are from the saved config.
    import json as _json
    cfg = _json.loads((Path.home() / ".config" / "rocketchat" / "config.json").read_text())
    members = []
    for k in ("admin_username", "bot_username"):
        u = cfg.get(k, "")
        if u and u not in members:
            members.append(u)

    # Try to find existing room (public channel or private group).
    room_id = ""
    is_group = False
    try:
        room_id = rc.get_channel_info(name)["channel"]["_id"]
        ok(f"public channel #{name} already exists (consider migrating to private group)")
    except Exception:
        pass
    if not room_id:
        try:
            room_id = rc._get("groups.info", admin=True, roomName=name)["group"]["_id"]
            is_group = True
            ok(f"private group #{name} already exists")
        except Exception:
            pass

    # Create the private group if neither exists.
    if not room_id:
        try:
            res = rc.create_group(name, members=members)
            room_id = res.get("group", {}).get("_id", "")
            is_group = True
            ok(f"created private group #{name} with members: {', '.join(members)}")
        except Exception as e:
            warn(f"could not create private group #{name}: {e}")
            return

    # Ensure admin + bot are members of the group (no-op for public channels).
    if is_group and room_id:
        for username in members:
            try:
                uid = rc.get_user_info(username)["user"]["_id"]
                rc._post("groups.invite", admin=True, roomId=room_id, userId=uid)
            except Exception as e:
                msg = str(e)
                if "already" in msg.lower() or "is-already" in msg.lower():
                    continue
                # Most "already in room" responses come back as 400; only warn on others.
                pass


def find_existing_webhook(rc, name: str, channel: str) -> str:
    """Return webhook URL for an existing integration matching this agent, or ''."""
    try:
        items = rc.list_webhooks()
    except Exception:
        return ""
    base_url = rc.base.rsplit("/api/", 1)[0]
    target_channels = {f"#{channel}", channel}
    for it in items:
        if it.get("type") != "webhook-incoming":
            continue
        ch = it.get("channel") or []
        if isinstance(ch, str): ch = [ch]
        if not (set(ch) & target_channels):
            continue
        if name not in (it.get("name") or "") and name not in (it.get("username") or ""):
            continue
        wid, tok = it.get("_id"), it.get("token")
        if wid and tok:
            return f"{base_url}/hooks/{wid}/{tok}"
    return ""


def register_webhook(rc, name: str, channel: str, webhook_name: str, dry: bool) -> str:
    if dry:
        info(f"[dry-run] register webhook for #{channel}")
        return f"https://example.invalid/hooks/<id>/<token>"
    existing = find_existing_webhook(rc, name, channel)
    if existing:
        ok(f"reusing existing webhook for #{channel}")
        return existing
    try:
        # Don't pass username=name — webhook poster must be a real RC user
        # (defaults to the bot account, e.g. 'Jarvis').
        res = rc.create_webhook(channel=f"#{channel}", name=webhook_name)
    except Exception as e:
        warn(f"webhook creation failed: {e}")
        return ""
    integ = res.get("integration", {})
    wid, tok = integ.get("_id"), integ.get("token")
    if not (wid and tok):
        warn(f"webhook response missing id/token: {res}")
        return ""
    base_url = rc.base.rsplit("/api/", 1)[0]
    url = f"{base_url}/hooks/{wid}/{tok}"
    ok(f"registered webhook {wid} for #{channel}")
    return url


# ─── scaffolding ────────────────────────────────────────────────────────────

def _render_tpl(tpl: Path, name: str, channel: str, ssh_host: str, session: str) -> str:
    """Render a template file substituting all {{}} placeholders."""
    return (tpl.read_text()
            .replace("{{name}}", name)
            .replace("{{channel}}", channel)
            .replace("{{ssh_host}}", ssh_host)
            .replace("{{session}}", session))


def render_context(name: str, channel: str, ssh_host: str, session: str, dest: Path, dry: bool):
    if dest.exists():
        ok(f"context.md already exists at {dest.relative_to(ROOT)} — leaving as-is")
        return
    if not CONTEXT_TPL.is_file():
        sys.exit(f"Missing template: {CONTEXT_TPL}")
    content = _render_tpl(CONTEXT_TPL, name, channel, ssh_host, session)
    if dry:
        info(f"[dry-run] write {dest.relative_to(ROOT)} ({len(content)} bytes)")
        return
    dest.write_text(content)
    ok(f"wrote {dest.relative_to(ROOT)}")


def scaffold_sandbox(name: str, channel: str, ssh_host: str, session: str,
                     agent_dir: Path, dry: bool):
    """Write per-agent Cursor sandbox files: rules + sandbox.json.

    Always rewrites both files so template changes propagate on re-deploy
    (these files are deploy-managed, not human-edited).
    """
    cursor_dir = agent_dir / ".cursor"
    rules_dir  = cursor_dir / "rules"
    rules_dest = rules_dir / "sandbox.mdc"
    json_dest  = cursor_dir / "sandbox.json"

    if dry:
        info(f"[dry-run] mkdir -p {rules_dir.relative_to(ROOT)}")
        info(f"[dry-run] write    {rules_dest.relative_to(ROOT)}")
        info(f"[dry-run] write    {json_dest.relative_to(ROOT)}")
        return

    if not SANDBOX_RULES_TPL.is_file():
        warn(f"Missing sandbox rules template: {SANDBOX_RULES_TPL} — skipping sandbox scaffold")
        return
    if not SANDBOX_JSON_TPL.is_file():
        warn(f"Missing sandbox.json template: {SANDBOX_JSON_TPL} — skipping sandbox scaffold")
        return

    rules_dir.mkdir(parents=True, exist_ok=True)
    rules_dest.write_text(_render_tpl(SANDBOX_RULES_TPL, name, channel, ssh_host, session))
    ok(f"wrote {rules_dest.relative_to(ROOT)}")

    json_text = SANDBOX_JSON_TPL.read_text().replace("{{jarvis_root}}", str(ROOT))
    json_dest.write_text(json_text)
    ok(f"wrote {json_dest.relative_to(ROOT)}")


def scaffold_dir_readme(tpl: Path, dest_dir: Path, name: str, channel: str,
                        ssh_host: str, session: str, dry: bool):
    """Create dest_dir and write its README.md from template (skip if README already exists)."""
    readme = dest_dir / "README.md"
    if dry:
        info(f"[dry-run] mkdir -p {dest_dir.relative_to(ROOT)}")
        if not readme.exists():
            info(f"[dry-run] write  {readme.relative_to(ROOT)}")
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    if readme.exists():
        ok(f"{readme.relative_to(ROOT)} already exists — leaving as-is")
        return
    if not tpl.is_file():
        warn(f"Template not found: {tpl} — skipping README")
        return
    readme.write_text(_render_tpl(tpl, name, channel, ssh_host, session))
    ok(f"wrote {readme.relative_to(ROOT)}")


def copy_and_inject_rc(name: str, channel: str, interval: int, system_prompt: str,
                      webhook_url: str, session: str, dest: Path, dry: bool):
    if not MASTER_RC.is_file():
        sys.exit(f"Missing master template: {MASTER_RC}")
    src = MASTER_RC.read_text()

    # Replace each DEFAULT_* line. They live in a known block right after CONFIG_FILE.
    def set_const(text: str, key: str, value: str) -> str:
        # value is a Python literal already (quoted/escaped)
        pat = re.compile(rf"^{key}\s*=\s*.*$", re.MULTILINE)
        if not pat.search(text):
            sys.exit(f"master-rocketchat.py is missing constant {key} (deploy injection point).")
        return pat.sub(f"{key} = {value}", text, count=1)

    def pylit(s: str) -> str:
        return repr(s)
    def pyint(n: int) -> str:
        return str(int(n))

    out = src
    out = set_const(out, "DEFAULT_CHANNEL",       pylit(f"#{channel}"))
    out = set_const(out, "DEFAULT_USER",          pylit(name))
    out = set_const(out, "DEFAULT_INTERVAL",      pyint(interval))
    out = set_const(out, "DEFAULT_WEBHOOK_URL",   pylit(webhook_url))
    out = set_const(out, "DEFAULT_TMUX_SESSION",  pylit(session))
    out = set_const(out, "DEFAULT_SYSTEM_PROMPT", pylit(system_prompt))

    if dry:
        info(f"[dry-run] write {dest.relative_to(ROOT)} ({len(out)} bytes, injected config for {name})")
        return
    dest.write_text(out)
    dest.chmod(0o755)
    ok(f"wrote {dest.relative_to(ROOT)} (config injected)")


# ─── mailinbox app ──────────────────────────────────────────────────────────

def _read_existing_const(path: Path, key: str) -> str:
    """Read a single DEFAULT_* constant from an existing file, returns '' if not found or empty."""
    if not path.is_file():
        return ""
    # Match KEY = 'value' or KEY = "value".
    # Use the quote char that opens the string to close it (avoids matching inline comments).
    m = re.search(rf"""^{key}\s*=\s*(['"])(.*?)\1""", path.read_text(), re.MULTILINE)
    return m.group(2) if m else ""


def copy_and_inject_mailinbox(host: str, email_addr: str, password: str,
                               dest: Path, dry: bool):
    """Copy mailinbox.py master to per-agent apps/ with credentials injected.

    Always refreshes the code from the master (picks up bug fixes), but
    rescues any existing credentials from the destination file when no
    explicit --mailinbox-* flags are supplied.
    """
    if not MASTER_MAILINBOX.is_file():
        warn(f"mailinbox.py master not found at {MASTER_MAILINBOX} — skipping")
        return

    # If no CLI credentials given, rescue them from the existing agent copy
    if not host and not email_addr and not password:
        host       = _read_existing_const(dest, "DEFAULT_HOST")
        email_addr = _read_existing_const(dest, "DEFAULT_EMAIL")
        password   = _read_existing_const(dest, "DEFAULT_PASSWORD")

    src = MASTER_MAILINBOX.read_text()

    def set_const(text: str, key: str, value: str) -> str:
        pat = re.compile(rf"^{key}\s*=\s*.*$", re.MULTILINE)
        if not pat.search(text):
            warn(f"mailinbox.py is missing constant {key} — skipping injection")
            return text
        return pat.sub(f"{key} = {value}", text, count=1)

    out = src
    out = set_const(out, "DEFAULT_HOST",     repr(host))
    out = set_const(out, "DEFAULT_EMAIL",    repr(email_addr))
    out = set_const(out, "DEFAULT_PASSWORD", repr(password))

    if dry:
        label = f"for {email_addr}" if email_addr else "no credentials"
        info(f"[dry-run] write {dest.relative_to(ROOT)} (mailinbox injected — {label})")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out)
    dest.chmod(0o755)
    if email_addr:
        ok(f"wrote {dest.relative_to(ROOT)} (mailinbox config injected for {email_addr})")
    else:
        info(f"wrote {dest.relative_to(ROOT)} (mailinbox stub — configure via dashboard or --mailinbox-* flags)")


# ─── browser app ────────────────────────────────────────────────────────────

def _browser_port_for(name: str) -> int:
    """Deterministic CDP port for an agent name, 9300-9999.

    Uses a stable Python-version-independent hash so port stays the same
    across re-deploys, app.py restarts, machine reboots — anything that
    depends on the name. Avoids ports 9222/9229 (Chrome / Node defaults)
    by starting at 9300.
    """
    import hashlib
    h = hashlib.sha1(name.encode("utf-8")).digest()
    return 9300 + (int.from_bytes(h[:4], "big") % 700)


def _detect_chrome_path() -> str:
    """Best-effort guess at the local system Chrome binary."""
    import platform as _pl
    sysname = _pl.system().lower()
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
    else:
        cands = []
    for c in cands:
        if Path(c).is_file():
            return c
    return ""


def copy_and_inject_browser(name: str, agent_dir: Path, dest: Path, dry: bool):
    """Copy browser.py master to per-agent apps/ with derived constants injected.

    Defaults are all derived from the agent name — no CLI flags needed:
      profile_dir = agents/<name>/browser-profile   (absolute path)
      cdp_port    = stable hash → 9300-9999
      chrome_path = system Chrome (auto-detected at deploy time)

    Existing constants are NOT preserved here (unlike mailinbox); they're
    derived deterministically so re-deploy always lands on the same values.
    Re-deploy is therefore a safe refresh — Chrome processes already running
    against the same port + profile keep working.
    """
    if not MASTER_BROWSER.is_file():
        warn(f"browser.py master not found at {MASTER_BROWSER} — skipping")
        return

    profile_dir = agent_dir / "browser-profile"
    cdp_port    = _browser_port_for(name)
    chrome_path = _detect_chrome_path()

    src = MASTER_BROWSER.read_text()

    def set_const(text: str, key: str, value: str) -> str:
        pat = re.compile(rf"^{key}\s*=\s*.*$", re.MULTILINE)
        if not pat.search(text):
            warn(f"browser.py is missing constant {key} — skipping injection")
            return text
        return pat.sub(f"{key} = {value}", text, count=1)

    out = src
    out = set_const(out, "DEFAULT_PROFILE_NAME", repr(name))
    out = set_const(out, "DEFAULT_PROFILE_DIR",  repr(str(profile_dir)))
    out = set_const(out, "DEFAULT_CDP_PORT",     str(cdp_port))
    out = set_const(out, "DEFAULT_CHROME_PATH",  repr(chrome_path))
    out = set_const(out, "DEFAULT_HEADLESS",     "False")

    if dry:
        info(f"[dry-run] write {dest.relative_to(ROOT)} (browser: port={cdp_port}, "
             f"profile={profile_dir.relative_to(ROOT)})")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out)
    dest.chmod(0o755)
    profile_dir.mkdir(parents=True, exist_ok=True)
    ok(f"wrote {dest.relative_to(ROOT)} (browser: port {cdp_port}, "
       f"profile {profile_dir.relative_to(ROOT)})")
    if not chrome_path:
        warn("No system Chrome detected on this machine — install Google Chrome "
             "or set DEFAULT_CHROME_PATH manually in the agent's browser.py")


# ─── tmux session ───────────────────────────────────────────────────────────

def tmux_session_exists(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


def kill_session(session: str, dry: bool):
    if not tmux_session_exists(session):
        return
    if dry:
        info(f"[dry-run] tmux kill-session -t {session}")
        return
    run(["tmux", "kill-session", "-t", session])
    ok(f"killed existing tmux session '{session}'")


def launch_session(session: str, agent_dir: Path, channel: str, interval: int,
                   system_prompt: str, attach: bool, dry: bool):
    model = read_agent_model(agent_dir)
    pane1 = (f'cd {agent_dir} && cursor agent --yolo --sandbox enabled '
             f'--model {model} "read context.md"')
    # Pane 2: run the monitor as a background child of the pane's shell so the
    # pane is usable as an interactive shell. No `nohup` — we WANT the monitor
    # to die when the pane (and thus its shell) dies. PYTHONUNBUFFERED=1 so the
    # log streams immediately.
    monitor_log = agent_dir / "logs" / "monitor.log"
    monitor_cmd = (
        f'PYTHONUNBUFFERED=1 python3 apps/rocketchat.py monitor "#{channel}" '
        f'--interval {interval} --tmux-session {session} '
        f'--persona {shell_quote(system_prompt)}'
    )
    # Run the monitor as a bg child, then drop the pane into an interactive
    # shell ($SHELL — NOT exec'd, so the outer shell stays alive to honor the
    # trap). When the pane dies, tmux SIGHUPs the outer shell, the trap fires,
    # the monitor gets killed. zsh does NOT SIGHUP bg jobs on exit by default,
    # so the explicit kill in the trap is what guarantees the kill cascade.
    pane2 = (
        f'cd {agent_dir} && '
        f'{monitor_cmd} >> {monitor_log} 2>&1 & '
        f'MON_PID=$!; '
        f'echo "Monitor PID: $MON_PID — log: {monitor_log}"; '
        f'trap "kill $MON_PID 2>/dev/null" EXIT HUP TERM INT; '
        f'$SHELL'
    )

    cmds = [
        ["tmux", "new-session", "-d", "-s", session, "-n", "main"],
        ["tmux", "set-option", "-t", session, "base-index", "1"],
        ["tmux", "set-option", "-t", session, "pane-base-index", "1"],
        # Run pane 1's command in the original (now pane 1) window.
        ["tmux", "send-keys", "-t", f"{session}:main", pane1, "Enter"],
        ["tmux", "split-window", "-t", f"{session}:main", "-v"],
        ["tmux", "send-keys", "-t", f"{session}:main.2", pane2, "Enter"],
    ]
    for c in cmds:
        run(c, dry=dry)
    ok(f"tmux session '{session}' launched (pane 1: cursor, pane 2: rc monitor)")

    if attach and not dry:
        os.execlp("tmux", "tmux", "attach-session", "-t", session)
    elif attach and dry:
        info(f"[dry-run] tmux attach-session -t {session}")


def shell_quote(s: str) -> str:
    """Single-quote-safe shell quoting for the persona string."""
    if not s:
        return "''"
    if "'" not in s:
        return f"'{s}'"
    return "'" + s.replace("'", "'\"'\"'") + "'"


# ─── ssh hint ───────────────────────────────────────────────────────────────

def ssh_host_known(name: str) -> bool:
    cfg = Path.home() / ".ssh" / "config"
    if not cfg.is_file():
        return False
    try:
        text = cfg.read_text()
    except Exception:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("host "):
            hosts = line.split()[1:]
            if name in hosts:
                return True
    return False


# ─── main ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="Agent name (also: SSH host, channel name, dir name)")
    p.add_argument("--interval", type=int, default=10, help="Monitor poll interval seconds (default: 10)")
    p.add_argument("--system-prompt", default=DEFAULT_PERSONA, help="Persona/system prompt for monitor")
    p.add_argument("--webhook-name", default="", help="Display name for the incoming webhook")
    p.add_argument("--no-webhook", action="store_true", help="Skip webhook registration")
    p.add_argument("--no-channel", action="store_true", help="Skip channel auto-create")
    p.add_argument("--no-attach",  action="store_true", help="Don't attach at the end")
    p.add_argument("--no-launch",  action="store_true",
                   help="Scaffold files only — do NOT kill/recreate tmux session")
    p.add_argument("--dry-run",    action="store_true", help="Print actions without executing")
    # mailinbox app credentials (per-agent)
    p.add_argument("--mailinbox-host",     default="", help="Mail-in-a-Box hostname (e.g. mail.example.com)")
    p.add_argument("--mailinbox-email",    default="", help="Agent's email address on the mail server")
    p.add_argument("--mailinbox-password", default="", help="IMAP/SMTP password for agent's mailbox")
    args = p.parse_args()

    name      = validate_name(args.name)
    channel   = name
    ssh_host  = name
    session   = session_for(name)
    agent_dir = AGENTS_DIR / name
    apps_dir  = agent_dir / "apps"
    logs_dir  = agent_dir / "logs"
    context_file = agent_dir / "context.md"
    rc_copy   = apps_dir / "rocketchat.py"
    dispatch_log = logs_dir / "dispatch.log"
    webhook_name = args.webhook_name or f"{name} webhook"

    print(f"\nDeploying agent: \033[1m{name}\033[0m  (session={session}, channel=#{channel})\n")

    if not ssh_host_known(name):
        warn(f"'{name}' not found in ~/.ssh/config — agent can still run, but SSH won't work until you add it")

    if args.dry_run:
        info("--- DRY RUN: no files written, no tmux sessions changed ---")

    # 1. Scaffold
    docs_dir      = agent_dir / "docs"
    utilities_dir = agent_dir / "utilities"
    routines_dir  = agent_dir / "routines"
    uploads_dir   = agent_dir / "uploads"
    downloads_dir = agent_dir / "downloads"
    if args.dry_run:
        info(f"[dry-run] mkdir -p {apps_dir.relative_to(ROOT)}")
        info(f"[dry-run] mkdir -p {logs_dir.relative_to(ROOT)}")
        info(f"[dry-run] mkdir -p {docs_dir.relative_to(ROOT)}")
        info(f"[dry-run] mkdir -p {utilities_dir.relative_to(ROOT)}")
        info(f"[dry-run] mkdir -p {routines_dir.relative_to(ROOT)}")
        info(f"[dry-run] mkdir -p {uploads_dir.relative_to(ROOT)}")
        info(f"[dry-run] mkdir -p {downloads_dir.relative_to(ROOT)}")
        info(f"[dry-run] touch    {dispatch_log.relative_to(ROOT)}")
    else:
        apps_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        docs_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        dispatch_log.touch(exist_ok=True)
    render_context(name, channel, ssh_host, session, context_file, args.dry_run)
    scaffold_dir_readme(UTILITIES_TPL, utilities_dir, name, channel, ssh_host, session, args.dry_run)
    scaffold_dir_readme(ROUTINES_TPL,  routines_dir,  name, channel, ssh_host, session, args.dry_run)
    scaffold_sandbox(name, channel, ssh_host, session, agent_dir, args.dry_run)

    # 2. RocketChat ops (channel + webhook) — needs config unless skipped
    webhook_url = ""
    need_rc = not (args.no_channel and args.no_webhook)
    if need_rc:
        if args.dry_run:
            info("[dry-run] would log in to RocketChat with master-rocketchat.py config")
            if not args.no_channel:
                ensure_channel(None, channel, dry=True)
            if not args.no_webhook:
                webhook_url = register_webhook(None, name, channel, webhook_name, dry=True)
        else:
            rc, _mod = _rc_client()
            if not args.no_channel:
                ensure_channel(rc, channel, dry=False)
            if not args.no_webhook:
                webhook_url = register_webhook(rc, name, channel, webhook_name, dry=False)

    # 3. Copy + inject per-agent rocketchat.py
    copy_and_inject_rc(name, channel, args.interval, args.system_prompt,
                       webhook_url, session, rc_copy, args.dry_run)

    # 3b. Copy + inject per-agent mailinbox.py
    mailinbox_copy = apps_dir / "mailinbox.py"
    copy_and_inject_mailinbox(
        host=args.mailinbox_host,
        email_addr=args.mailinbox_email,
        password=args.mailinbox_password,
        dest=mailinbox_copy,
        dry=args.dry_run,
    )

    # NOTE: browser.py is intentionally NOT auto-installed at deploy time.
    # It's an opt-in app — users enable it per-agent through the dashboard's
    # Add App modal, which calls /api/apps/install. That path auto-derives
    # the (port, profile dir, chrome path) defaults from the agent name, so
    # there's still zero typing for the user — but the file only exists on
    # agents the user has actually turned the browser on for.

    # 4. tmux: kill + recreate (skipped when --no-launch)
    if args.no_launch:
        info("--no-launch given: skipping tmux kill/recreate (scaffold only)")
    else:
        kill_session(session, args.dry_run)
        launch_session(session, agent_dir, channel, args.interval, args.system_prompt,
                       attach=(not args.no_attach), dry=args.dry_run)

    print()
    if args.dry_run:
        info("Dry run complete.")
    elif args.no_launch:
        ok(f"Agent {name} scaffolded (tmux not started).")
    else:
        ok(f"Agent {name} deployed.  tmux attach -t {session}")


if __name__ == "__main__":
    main()
