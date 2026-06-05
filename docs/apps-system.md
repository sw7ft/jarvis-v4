# Agent Apps — complete guide

JARVIS v4 **apps** are small, self-contained CLI tools that live in each agent's
`apps/` directory. They give the Cursor agent (and human operators) structured
ways to talk to Rocket.Chat, email, Chrome, and anything else you add.

This document explains the full lifecycle: master copies, credential injection,
deploy vs dashboard install, how agents invoke apps, and how to build a new one.

**Related docs:** [mailinbox.md](mailinbox.md) · [browser.md](browser.md) ·
[rocketchat-integration.md](rocketchat-integration.md) · [deploy-and-apps.md](deploy-and-apps.md)

---

## Apps vs modules vs utilities

These three extension points are often confused. They solve different problems:

| Concept | Lives in | Runs on | Who installs | Typical use |
|---------|----------|---------|--------------|-------------|
| **App** | `agents/<name>/apps/*.py` | MacBook (agent host) | `deploy.py` or dashboard | RC, email, browser — agent runs CLI locally |
| **Module** | `modules/<name>/` | Remote server (or copied to agent dir) | Agent follows `MODULE.md` | PHP contact forms, server-side handlers |
| **Utility** | `agents/<name>/utilities/` | Wherever agent SSHs | Agent writes over time | One-off scripts for a specific client |

**Apps** are first-class in the framework: registry, dashboard UI, inject pipeline.
**Modules** are optional packages the agent deploys manually to client infrastructure.
**Utilities** are ad-hoc tools with no central registry.

---

## Mental model

```
jarvisv4/apps/                          ← masters (templates, never run by agents)
├── master-rocketchat.py
├── mailinbox.py
└── browser.py
        │
        │  deploy.py copy + inject          dashboard /api/apps/install
        │  (on every deploy)                (opt-in, no tmux restart)
        ▼
agents/example.com/apps/                ← per-agent copies (agent runs these)
├── rocketchat.py    ← always present after deploy
├── mailinbox.py     ← present after deploy (may have empty credentials)
└── browser.py         ← only if installed via dashboard (opt-in)
        │
        │  agent working dir = agents/example.com/
        ▼
python3 apps/rocketchat.py send "#example.com" "Done."
python3 apps/mailinbox.py inbox
python3 apps/browser.py goto https://example.com
```

Every app follows the same contract:

1. **Master** in `jarvisv4/apps/` with `DEFAULT_*` constants at the top (empty or stub values).
2. **Injection** replaces those lines with per-agent values before writing the copy.
3. **Runtime** — agent `cd`s to its directory and runs `python3 apps/<app>.py <subcommand>`.
4. **Never run the master directly** from an agent context (except RC admin tasks on `master-rocketchat.py`).

---

## The three built-in apps

### Rocket.Chat (`rocketchat.py`) — builtin, always installed

| Property | Value |
|----------|-------|
| Master | `apps/master-rocketchat.py` |
| Agent copy | `agents/<name>/apps/rocketchat.py` |
| Installed by | Every `deploy.py` run |
| Removable | No (`builtin: True`) |
| Secrets | Shared in `~/.config/rocketchat/config.json` |
| Per-agent config | Channel, webhook URL, poll interval, tmux session, persona |

**Two-layer credentials:**

```
~/.config/rocketchat/config.json     ← ONE file for the whole fleet
  url, admin_username, admin_password
  bot_username, bot_password

agents/<name>/apps/rocketchat.py     ← per-agent constants (injected)
  DEFAULT_CHANNEL      = '#example.com'
  DEFAULT_USER         = 'example.com'
  DEFAULT_INTERVAL     = 10
  DEFAULT_WEBHOOK_URL  = 'https://chat.example.com/hooks/...'
  DEFAULT_TMUX_SESSION = 'example-com'
  DEFAULT_SYSTEM_PROMPT = '...'
```

The monitor (`python3 apps/rocketchat.py monitor`) runs in **tmux pane 2** and
polls the channel. It does not call an LLM — it forwards human messages into
pane 1 via `tmux send-keys`. See [rocketchat-integration.md](rocketchat-integration.md).

**Key commands (from agent dir):**

```bash
python3 apps/rocketchat.py send "#example.com" "Hello"
python3 apps/rocketchat.py history --count 20
python3 apps/rocketchat.py files
python3 apps/rocketchat.py download <url> --dest downloads/file.pdf
```

**Admin-only (from repo root, master script):**

```bash
python3 apps/master-rocketchat.py setup
python3 apps/master-rocketchat.py webhooks
python3 apps/master-rocketchat.py channels
```

---

### Mail Inbox (`mailinbox.py`) — optional credentials

| Property | Value |
|----------|-------|
| Master | `apps/mailinbox.py` |
| Installed by | Every deploy (file always copied) |
| Configured when | `--mailinbox-*` flags, dashboard, or `mailinbox.py setup` |
| Removable | Yes (dashboard Remove) |
| Secrets | Baked into agent copy (`DEFAULT_PASSWORD`) |

On every deploy, `copy_and_inject_mailinbox()` refreshes code from master. If you
do **not** pass `--mailinbox-*` flags, it **rescues** existing credentials from
the current agent file so redeploy never wipes passwords.

```bash
# First-time with credentials
python3 deploy.py example.com \
  --mailinbox-host mail.example.com \
  --mailinbox-email agent@example.com \
  --mailinbox-password 'secret'

# Redeploy without flags → credentials preserved, code updated
python3 deploy.py example.com --no-channel --no-webhook --no-attach
```

Dashboard shows the mail icon only when `DEFAULT_EMAIL` is non-empty
(`has_mailinbox` in `get_agents()`).

Full CLI: [mailinbox.md](mailinbox.md)

---

### Browser (`browser.py`) — opt-in only

| Property | Value |
|----------|-------|
| Master | `apps/browser.py` |
| Installed by | Dashboard **+ → Browser → Install** only |
| NOT installed by | `deploy.py` (intentionally skipped) |
| Removable | Yes |
| Secrets | None (profile dir on disk) |
| Derived config | Port, profile path, Chrome path — computed from agent name |

Browser is different from mail:

- **No auto-install on deploy** — avoids copying Playwright/Chrome config to
  agents that will never use it.
- **All constants derived** — no typing required. Port = SHA-1 hash of agent
  name → 9300–9999. Profile = `agents/<name>/browser-profile/`.
- **Persistent process** — Chrome stays running between commands; cookies and
  logins survive in the profile directory.

Install via dashboard or API:

```bash
curl -X POST http://localhost:5112/api/apps/install \
  -H 'Content-Type: application/json' \
  -d '{"agent":"example.com","app_id":"browser","fields":{}}'
```

Empty `fields` is fine — server fills derived defaults.

Full CLI + context files: [browser.md](browser.md)

---

## Install paths compared

| App | `deploy.py` | Dashboard install | tmux restart on install |
|-----|-------------|-------------------|-------------------------|
| rocketchat | Always | N/A (builtin) | Yes (full deploy restarts tmux) |
| mailinbox | Always copies file | Save Config | No |
| browser | **Never** | Install button | No |

---

## Credential injection — how it works

Both `deploy.py` and `app.py`'s `inject_app()` use the same mechanism:

1. Read master `.py` as text.
2. For each `DEFAULT_*` key, find the line `^KEY = .*$` (multiline regex).
3. Replace with `KEY = <python-literal>`.
4. Write to `agents/<name>/apps/<dest>` and `chmod 755`.

Example transformation:

```python
# Master (apps/mailinbox.py)
DEFAULT_EMAIL = ""

# After injection (agents/example.com/apps/mailinbox.py)
DEFAULT_EMAIL = 'ops@example.com'
```

### Preservation rules

| App | On redeploy without new flags |
|-----|-------------------------------|
| rocketchat | Re-injected from deploy args (channel, webhook, etc.) |
| mailinbox | **Rescues** HOST, EMAIL, PASSWORD from existing file |
| browser | **Re-derived** from agent name (deterministic, same values every time) |

### Dashboard field resolution (`POST /api/apps/install`)

For each field, in order:

1. User-typed value (if non-empty).
2. Secret fields showing `****` → keep existing value unchanged.
3. Existing value in installed file.
4. Server-derived default (`_derived_app_defaults()` — browser only).

Install **never** restarts tmux — safe on live agents.

---

## APPS_REGISTRY — the single source of truth

Defined at the top of `app.py`. Each entry drives the dashboard Add App modal,
install/remove API, config popover, and card dock icons.

```python
APPS_REGISTRY = {
    "rocketchat": {
        "label":   "Rocket.Chat",
        "master":  "master-rocketchat.py",
        "dest":    "rocketchat.py",
        "color":   "#f5455c",
        "builtin": True,
        "fields": [
            {"key": "DEFAULT_CHANNEL", "label": "Channel", "secret": False},
            # ...
        ],
    },
    "mailinbox": { ... },
    "browser":   { ... },
}
```

| Field | Purpose |
|-------|---------|
| `label` | Display name in UI |
| `master` | Filename under `jarvisv4/apps/` |
| `dest` | Filename under `agents/<name>/apps/` |
| `color` | Hex color for sidebar dot and card icon |
| `builtin` | If `True`, cannot remove; always considered core |
| `fields` | Config keys shown in modal; `secret: True` masks in API responses |

Adding a new app = one registry entry + master file + (optionally) deploy hook.

---

## Dashboard integration

### Agent card dock (right panel)

| Icon | Condition | Click action |
|------|-----------|--------------|
| RC bubble | Always | Popover: channel, webhook, kill/restart monitor |
| Mail | `has_mailinbox` | Popover: config + live connection test |
| Browser | `has_browser` | Popover: status, launch/stop, screenshot |
| **+** | Always | Add App modal |

`has_mailinbox` / `has_browser` are computed by reading injected constants in
the agent's app file — not merely "file exists".

### Add App modal

- Left: all apps from registry with installed badge.
- Right: form fields from `fields` list.
- **Install / Save Config** → `POST /api/apps/install`
- **Remove** → `POST /api/apps/remove` (blocked for `builtin`)

### API routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/registry` | GET | All apps + field definitions |
| `/api/apps/installed/<name>` | GET | Which app files exist |
| `/api/apps/config/<name>/<app_id>` | GET | Current constants (secrets masked) |
| `/api/apps/install` | POST | Copy master + inject |
| `/api/apps/remove` | POST | Delete app file |
| `/api/mailinbox/test` | POST | Run mailinbox test subprocess |
| `/api/browser/status` | GET | Browser status JSON |
| `/api/browser/launch` | POST | Launch Chrome |
| `/api/browser/stop` | POST | Stop Chrome |
| `/api/browser/screenshot` | GET | PNG screenshot |

Full list: [api-reference.md](api-reference.md)

---

## How agents use apps

Agents read `context.md`, which documents RC reply patterns and lists available
apps. The agent's **working directory** is always `agents/<name>/`:

```bash
# Correct — relative to agent dir
python3 apps/rocketchat.py send "#example.com" "Task complete."

# Wrong — do not run from repo root
python3 agents/example.com/apps/rocketchat.py send ...
```

### Dispatch flow (RC app)

```
Human posts in #example.com
    → monitor polls (apps/rocketchat.py monitor)
    → tmux send-keys to pane 1 (Cursor agent)
    → agent thinks, runs tools, maybe other apps
    → python3 apps/rocketchat.py send "#example.com" "reply"
    → dispatch.log records event: send
```

### STOP signal

Standalone `STOP`, `HALT`, or `ABORT` in RC triggers Ctrl-C in pane 1 without
dispatching. See [stop-signal.md](stop-signal.md).

### Audit trail

`logs/dispatch.log` — JSON lines for `dispatch`, `send`, `stop`. Agents and
operators use this to recover context after restarts.

---

## deploy.py app steps (every run)

Order inside `main()`:

1. **Scaffold** — `apps/`, `logs/`, `docs/`, etc.
2. **RC channel + webhook** (unless `--no-channel` / `--no-webhook`)
3. **`copy_and_inject_rc()`** — always
4. **`copy_and_inject_mailinbox()`** — always (credentials optional/rescued)
5. **Browser** — explicitly **not** called (opt-in via dashboard)
6. **tmux** kill + recreate (unless `--no-launch`)

Useful flags:

```bash
python3 deploy.py example.com --dry-run              # print plan only
python3 deploy.py example.com --no-attach            # don't attach tmux
python3 deploy.py example.com --no-launch            # scaffold + inject only
python3 deploy.py example.com --no-channel --no-webhook  # skip RC server ops
```

---

## Adding a new app (checklist)

### 1. Write the master script

Create `jarvisv4/apps/myapp.py`:

```python
#!/usr/bin/env python3
"""myapp.py — short description."""

DEFAULT_API_URL = ""      # injected by deploy / dashboard
DEFAULT_API_KEY = ""      # injected; mark secret in registry

import argparse
# ... CLI implementation ...
```

Rules:

- Put all injectable config in `DEFAULT_*` lines near the top (after imports/docstring).
- Support `--help` and meaningful exit codes.
- Prefer stdlib; add deps to `requirements.txt`.
- Document subcommands in a module docstring.

### 2. Add to APPS_REGISTRY

In `app.py`:

```python
"myapp": {
    "label":   "My App",
    "master":  "myapp.py",
    "dest":    "myapp.py",
    "color":   "#22c55e",
    "builtin": False,
    "fields": [
        {"key": "DEFAULT_API_URL", "label": "API URL",  "secret": False},
        {"key": "DEFAULT_API_KEY", "label": "API Key",  "secret": True},
    ],
},
```

Restart `app.py`. The Add App modal picks it up automatically.

### 3. Optional: deploy.py integration

If the app should refresh on every deploy (like mailinbox):

```python
def copy_and_inject_myapp(api_url: str, api_key: str, dest: Path, dry: bool):
    if not api_url and not api_key:
        api_url = _read_existing_const(dest, "DEFAULT_API_URL")
        api_key = _read_existing_const(dest, "DEFAULT_API_KEY")
    # ... same set_const pattern as mailinbox ...
```

Call from `main()` and add CLI flags if needed.

If the app is **opt-in only** (like browser), skip deploy — dashboard install is enough.

### 4. Optional: derived defaults

If config can be computed from agent name, add logic to `_derived_app_defaults()`
in `app.py` (mirror `copy_and_inject_*` in `deploy.py` if you also want deploy support).

### 5. Dashboard UX (optional)

- Add `has_myapp` helper + field on agent dict in `get_agents()`.
- Add icon block in `makeCard()` JS (copy mail/browser pattern).
- Add popover or API route if the app needs live actions (test, status).

### 6. Document

- `docs/myapp.md` — CLI reference
- Update `templates/agent-context.md` Apps table
- Row in [docs/README.md](README.md)

---

## Security notes

| Risk | Mitigation |
|------|------------|
| Mail passwords in agent files | Gitignore `agents/*/apps/`; never commit |
| RC bot password | Only in `~/.config/rocketchat/config.json` (chmod 600) |
| Dashboard exposes secrets | API masks `secret: True` fields with asterisks |
| Browser profile | Gitignore `agents/*/browser-profile/` — may contain session cookies |
| Shared RC config | One bot account serves all agents — channel isolation is the boundary |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| App not in Add App modal | Master file missing or `app.py` not restarted | Check `apps/<master>` exists; restart dashboard |
| Mail icon missing | Empty `DEFAULT_EMAIL` | Install/save config with real email |
| Browser icon missing | `browser.py` not installed | Dashboard → + → Browser → Install |
| `send` goes to wrong channel | Omitted channel arg | Always pass `"#channel"` explicitly |
| Redeploy wiped mail password | Passed empty `--mailinbox-password` | Omit flags to rescue, or set via dashboard |
| Monitor not dispatching | Stale monitor code | Redeploy or dashboard RC restart |
| `ModuleNotFoundError: httpx` | venv not active | `pip install -r requirements.txt` |

More: [troubleshooting.md](troubleshooting.md)

---

## Quick reference

```bash
# List what's installed for an agent
ls agents/example.com/apps/

# Read injected constants
grep '^DEFAULT_' agents/example.com/apps/rocketchat.py

# Test mail from agent dir
cd agents/example.com && python3 apps/mailinbox.py test

# Test browser
cd agents/example.com && python3 apps/browser.py test

# Dashboard registry
curl -s http://localhost:5112/api/apps/registry | jq .
```
