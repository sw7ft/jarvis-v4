# Agents

Each JARVIS agent is a directory under `agents/<agent.name>/`. This document
describes the layout, lifecycle, and conventions.

---

## Directory layout

```
agents/<agent.name>/
├── context.md              ← Primary instructions (from template, then edited)
├── tags.json               ← Optional dashboard tags (array of strings)
├── .cursor-model           ← Optional Cursor model override (single line slug)
├── .cursor/
│   ├── sandbox.json        ← Filesystem sandbox (deploy-managed)
│   └── rules/sandbox.mdc   ← Scope rules for the model
├── apps/
│   ├── rocketchat.py       ← Injected RC copy (always after deploy)
│   ├── mailinbox.py        ← Copied on deploy; credentials optional
│   └── browser.py          ← Optional (dashboard install only)
├── browser-profile/        ← Chrome user data (browser app, gitignored)
├── logs/
│   ├── dispatch.log        ← JSON-Lines: dispatch, send, stop events
│   └── monitor.log         ← Monitor stdout/stderr
├── docs/                   ← Deep reference the agent writes
├── utilities/              ← One-off scripts
│   └── README.md
├── routines/               ← Scheduled / recurring tasks
│   └── README.md
├── uploads/                ← Incoming files
└── downloads/              ← Artifacts the agent saves
```

Reference: `agents/_example/` in this repo.

---

## Naming

| Agent name | tmux session | RC channel |
|------------|--------------|------------|
| `example.com` | `example-com` | `#example.com` |
| `my-client` | `my-client` | `#my-client` |

Rules: start with alphanumeric; then alnum, `.`, `-`, `_` allowed.

---

## context.md

Rendered from `templates/agent-context.md` on **first deploy only** (existing
files are preserved on redeploy).

Contains:

- Identity table (name, SSH, channel, session, working dir)
- How to receive/send RC messages
- Dispatch log usage
- SSH and scope reminders

**You** (or the agent) append client-specific knowledge here over time.

---

## Lifecycle

| Action | Command / UI |
|--------|--------------|
| Create | `python3 deploy.py <name>` |
| Redeploy (refresh apps, restart tmux) | `python3 deploy.py <name>` or dashboard Refresh |
| Scaffold only | `python3 deploy.py <name> --no-launch` |
| Stop | Dashboard Stop or `tmux kill-session -t <session>` |
| Start | Dashboard Start |
| Archive | Dashboard → move to `archive/` |

Redeploy **kills and recreates** the tmux session by default.

---

## Model selection

Default model: `composer-2.5` (in `deploy.py`).

Per-agent override:

```bash
echo "composer-2" > agents/example.com/.cursor-model
python3 deploy.py example.com   # refresh session to apply
```

Dashboard Agent Settings can also write this file.

---

## Tags

`agents/<name>/tags.json` — JSON array of lowercase strings for dashboard
filtering:

```json
["production", "always-on"]
```

Tag `always-on` can exclude agents from auto-hibernate (if enabled in your install).

---

## Logs

### dispatch.log

```bash
tail -f agents/example.com/logs/dispatch.log | jq .
```

Events: `dispatch`, `send`, `stop`.

### monitor.log

Stdout from pane 2 monitor — useful when messages aren't dispatching.

---

## Supervisor agent

Optional meta-agent named `supervisor` that uses `apps/master-rocketchat.py`
directly for fleet admin. Deploy like any other agent but uses master RC script
for channel/webhook ops across the fleet.

---

## What not to commit

Production installs should gitignore:

- `agents/*/apps/` (injected credentials)
- `agents/*/logs/`
- `agents/*/browser-profile/`

This open-source repo only ships `agents/_example/`.
