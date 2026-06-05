# Deploy & Apps System — JARVIS v4

> **Deep dive:** See **[apps-system.md](apps-system.md)** for the complete guide —
> apps vs modules, all three built-in apps, APPS_REGISTRY, dashboard API,
> credential preservation, and how to add a new app.

## Overview

Each JARVIS v4 agent is a self-contained directory under `agents/<name>/` with its own copies of all app scripts, pre-configured with per-agent credentials. `deploy.py` is the single tool that creates and refreshes agents. The dashboard (`app.py`) provides a web UI to manage apps on running agents without touching tmux.

---

## Directory Structure

```
jarvisv4/
├── app.py                        ← Dashboard (Flask, port 5112)
├── deploy.py                     ← Agent lifecycle manager
├── apps/
│   ├── master-rocketchat.py      ← RC master (never run directly)
│   ├── mailinbox.py              ← Mail master (optional)
│   └── browser.py                ← Browser master (optional)
├── templates/
│   ├── agent-context.md          ← context.md template
│   ├── utilities/README.md       ← utilities/ README template
│   └── routines/README.md        ← routines/ README template
├── docs/                         ← System-wide docs (this file)
└── agents/
    └── <name>/
        ├── context.md            ← Agent instructions (rendered from template)
        ├── apps/
        │   ├── rocketchat.py     ← Per-agent RC copy (credentials injected)
        │   └── mailinbox.py      ← Per-agent mail copy (credentials injected)
        ├── logs/
        │   ├── dispatch.log      ← All inbound/outbound message events
        │   └── monitor.log       ← RC monitor stdout
        ├── docs/                 ← Agent-specific deep knowledge
        ├── utilities/            ← One-off scripts the agent builds
        │   └── README.md
        └── routines/             ← Recurring scheduled tasks
            └── README.md
```

---

## deploy.py — What it does

`deploy.py` is the single entry point for creating or refreshing an agent. It always runs in full — there is no partial deploy.

### What happens on every deploy

1. **Scaffold** — creates `apps/`, `logs/`, `docs/`, `utilities/`, `routines/` if missing. Writes `context.md` from template (skips if already exists). Writes `utilities/README.md` and `routines/README.md` from templates (skips if already exist).
2. **RocketChat ops** — ensures the RC channel exists, registers/updates the incoming webhook.
3. **Copy + inject `rocketchat.py`** — always refreshes from master, injects per-agent constants (channel, session, webhook URL, interval, system prompt).
4. **Copy + inject `mailinbox.py`** — always refreshes from master, rescues existing credentials from the current agent copy before overwriting (see Credential Preservation below).
5. **Browser** — **not** auto-installed; opt-in via dashboard **+ → Browser → Install** (see [apps-system.md](apps-system.md)).
6. **tmux** — kills existing session and launches fresh (pane 1: cursor agent, pane 2: RC monitor).

### Basic usage

```bash
# New agent
python3 deploy.py <agent.name>

# New agent with mail credentials
python3 deploy.py <agent.name> \
  --mailinbox-host mail.example.com \
  --mailinbox-email agent@mail.example.com \
  --mailinbox-password '<password>'

# Refresh existing agent (restart tmux, re-inject apps)
python3 deploy.py <agent.name> --no-channel --no-webhook --no-attach

# Dry run (print what would happen, no changes)
python3 deploy.py <agent.name> --dry-run
```

---

## Apps System

### How apps work

Every app follows the same pattern:

1. A **master copy** lives in `jarvisv4/apps/` with empty `DEFAULT_*` stubs
2. `deploy.py` copies it to `agents/<name>/apps/` and injects per-agent values into those stubs
3. The agent runs the script from its working directory: `python3 apps/<app>.py <command>`
4. The master is never run directly — it is only a source template

### App Registry (`APPS_REGISTRY` in `app.py`)

The dashboard's `APPS_REGISTRY` dict describes every installable app and its config fields. This drives the "Add App" modal UI, the config popover, and the install/remove API routes.

Adding a new app requires:
1. Write the master `.py` in `jarvisv4/apps/` with `DEFAULT_*` stubs at the top
2. Add a `copy_and_inject_<app>()` function to `deploy.py`
3. Call it from `main()` in `deploy.py`
4. Add an entry to `APPS_REGISTRY` in `app.py`

---

## Credential Injection & Preservation

### How credentials get into each agent copy

`deploy.py` uses regex substitution to replace `DEFAULT_*` constant lines in the master source before writing the agent copy:

```python
# Master (jarvisv4/apps/mailinbox.py)
DEFAULT_HOST     = ""   # injected by deploy.py
DEFAULT_EMAIL    = ""
DEFAULT_PASSWORD = ""

# Agent copy (agents/<name>/apps/mailinbox.py) after deploy
DEFAULT_HOST     = 'mail.example.com'
DEFAULT_EMAIL    = 'agent@mail.example.com'
DEFAULT_PASSWORD = '<your-password>'
```

### Credential preservation on redeploy

When `deploy.py` runs without `--mailinbox-*` flags (e.g. triggered by dashboard Start/Refresh), it **rescues existing credentials** from the current agent copy before overwriting:

```
deploy.py (no --mailinbox-* flags)
  → _read_existing_const() reads DEFAULT_HOST/EMAIL/PASSWORD from current file
  → re-injects those values into fresh master code
  → agent gets updated code + original credentials intact
```

This means:
- Bug fixes to the master always propagate to agents on next deploy
- Credentials are never lost on redeploy unless you explicitly pass new `--mailinbox-*` flags

### To update credentials on a running agent

Use the dashboard "Add App" modal → Mail Inbox tab → edit fields → Save Config. This calls `POST /api/apps/install` which writes only the app file, no tmux restart.

Or via CLI:

```bash
python3 deploy.py <name> --mailinbox-host mail.example.com \
  --mailinbox-email agent@mail.example.com --mailinbox-password '<new-pass>' \
  --no-channel --no-webhook --no-attach
```

---

## Dashboard App Management

The dashboard at `http://localhost:5112` provides:

- **App dock** on each agent card — shows icons for installed apps (RC always present, mail shown if installed)
- **`+` button** — opens the App Manager modal for that agent
- **App Manager modal** — tabbed interface showing all apps in the registry:
  - Installed apps show a `✓` and pre-fill current config (passwords masked)
  - Non-installed apps show empty form fields
  - "Save Config" / "Install" writes the app file without restarting tmux
  - "Remove" deletes the app file (builtin apps like RC cannot be removed)
- **RC icon popover** — click the red RC icon to see channel config, kill/restart monitor
- **Mail icon popover** — click the blue mail icon to see mail config and run a live connection test

### API routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/apps/registry` | GET | All registered apps + field definitions |
| `/api/apps/installed/<name>` | GET | Which apps are installed for an agent |
| `/api/apps/config/<name>/<app_id>` | GET | Current config values (passwords masked) |
| `/api/apps/install` | POST | Copy + inject app file (no tmux restart) |
| `/api/apps/remove` | POST | Delete app file from agent |
| `/api/mailinbox/test` | POST | Run `mailinbox.py test` and return output |
| `/api/rc/config/<name>` | GET | RC config constants for an agent |
| `/api/rc/kill` | POST | Kill RC monitor process in pane 2 |
| `/api/rc/restart` | POST | Restart RC monitor in pane 2 |

---

## Rocketchat vs Mailinbox — Config differences

| | rocketchat.py | mailinbox.py |
|---|---|---|
| Shared server credentials | `~/.config/rocketchat/config.json` | N/A (each agent has own account) |
| Per-agent constants injected | Channel, session, webhook URL, interval | Host, email, password, inbox |
| Sensitive secrets in agent file | No (bot password in shared config) | Yes (password baked in) |
| Credential rescue on redeploy | N/A (non-sensitive only) | Yes — rescued from existing file |
| Setup | `python3 apps/master-rocketchat.py setup` | Dashboard or `--mailinbox-*` flags |

---

## Adding a New App (checklist)

1. Create `jarvisv4/apps/<appname>.py` with `DEFAULT_*` stubs and a docstring explaining setup
2. Add `copy_and_inject_<appname>()` to `deploy.py` (include credential rescue if secrets are baked in)
3. Add `--<appname>-*` CLI args to `deploy.py`'s argparse and call the inject function from `main()`
4. Add entry to `APPS_REGISTRY` in `app.py` with label, master filename, dest filename, color, fields list
5. Add icon CSS class to `app.py` if a unique brand color is needed
6. Add the icon JS block to `makeCard()` in `app.py` (conditioned on `data.has_<appname>`)
7. Add `"has_<appname>"` to the agent dict in `get_agents()` in `app.py`
8. Add a popover or reuse the App Manager modal for config viewing/editing
9. Document in `docs/<appname>.md`
