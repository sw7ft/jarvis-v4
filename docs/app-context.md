# JARVIS v4 Dashboard — `app.py`

Single-file Flask web UI for monitoring and managing all JARVIS v4 agents.

```
Run:  python3 app.py
URL:  http://localhost:5112
```

---

## Architecture Overview

```
app.py (Flask + flask-sock)
│
├── API routes          /api/*                Python → tmux / filesystem / deploy.py
├── WebSocket route     /ws/tmux/<session>    PTY bridge for xterm.js terminal
└── Dashboard HTML      inline DASHBOARD_HTML  rendered via render_template_string
```

Everything lives in one file — HTML, CSS, and JavaScript are inline strings
rendered by Flask. There is no build step, no bundler, no separate templates.

---

## Key Constants (top of file)

| Constant       | Value / Path                        | Purpose                          |
|----------------|-------------------------------------|----------------------------------|
| `JARVIS_ROOT`  | directory of `app.py`               | Project root                     |
| `AGENTS_DIR`   | `jarvisv4/agents/`                  | Live agent directories           |
| `ARCHIVE_DIR`  | `jarvisv4/archive/`                 | Archived agents                  |
| `APPS_DIR`     | `jarvisv4/apps/`                    | Master app files                 |
| `DEPLOY_PY`    | `jarvisv4/deploy.py`                | Agent lifecycle script           |
| `APPS_REGISTRY`| dict in `app.py` (top of file)      | All installable apps             |

---

## App Registry

`APPS_REGISTRY` is a Python dict that defines every installable app. Each entry:

```python
"mailinbox": {
    "label":   "Mail Inbox",          # display name in UI
    "master":  "mailinbox.py",        # source file in jarvisv4/apps/
    "dest":    "mailinbox.py",        # destination in agents/<name>/apps/
    "color":   "#38bdf8",             # colour dot in sidebar
    "builtin": False,                 # if True, always present, can't be removed
    "fields": [                       # config constants shown in the Add App modal
        {"key": "DEFAULT_HOST",  "label": "Mail Host", "secret": False},
        {"key": "DEFAULT_EMAIL", "label": "Email",     "secret": False},
        ...
    ],
}
```

**To add a new app:**
1. Place its master `.py` file in `jarvisv4/apps/`.
2. Add one entry to `APPS_REGISTRY` in `app.py`.
3. The dashboard's Add App modal and install/remove/config API routes
   all work automatically — no further changes needed.

---

## Agent Data Model

`get_agents()` returns a list of dicts, one per agent directory under `agents/`:

| Field            | Source                                           |
|------------------|--------------------------------------------------|
| `name`           | directory name                                   |
| `session`        | name with `.` → `-` (tmux session name)          |
| `online`         | whether tmux session exists                      |
| `dispatches`     | count of `"event":"dispatch"` lines in dispatch.log |
| `monitor_status` | `alive` / `stale` / `dead` / `none`              |
| `monitor_age`    | minutes since last monitor heartbeat             |
| `has_mailinbox`  | True if mailinbox.py exists with non-empty email |
| `tags`           | list from `agents/<name>/tags.json`              |

### Monitor Status Logic (`monitor_heartbeat`)

1. Check if `rocketchat.py monitor` is running in pane 2 of the agent's tmux session.
2. If the process is found → `alive`.
3. Otherwise fall back to `monitor.log` / `dispatch.log` mtime:
   - < 15 min ago → `stale`
   - ≥ 15 min ago → `dead`

---

## API Routes

### Agent lifecycle
| Method | Route                          | Action                                      |
|--------|--------------------------------|---------------------------------------------|
| GET    | `/api/agents`                  | List all agents with full data              |
| POST   | `/api/agent/stop`              | Kill tmux session                           |
| POST   | `/api/agent/start`             | Run `deploy.py <name> --no-attach`          |
| POST   | `/api/agent/refresh`           | Stop + start (context reset)                |
| GET    | `/api/pane/snapshots`          | Latest pane 1 text for agent cards          |
| GET    | `/api/agent/ctx/<name>`        | Context window % from pane 1 output         |
| GET    | `/api/agent/tags/<name>`       | Read tags                                   |
| POST   | `/api/agent/tags/<name>`       | Write tags                                  |
| POST   | `/api/agent/archive`           | Move to `archive/`, kill session            |
| POST   | `/api/agent/restore`           | Move from `archive/` back to `agents/`      |

### App management
| Method | Route                               | Action                                   |
|--------|-------------------------------------|------------------------------------------|
| GET    | `/api/apps/registry`                | Return `APPS_REGISTRY` (public fields)   |
| GET    | `/api/apps/installed/<name>`        | Which apps exist for agent               |
| GET    | `/api/apps/config/<name>/<app_id>`  | Read current `DEFAULT_*` constants       |
| POST   | `/api/apps/install`                 | Copy master → agent, inject constants    |
| POST   | `/api/apps/remove`                  | Delete app file from agent               |

### RC monitor control
| Method | Route                              | Action                                    |
|--------|------------------------------------|-------------------------------------------|
| GET    | `/api/rc/config/<name>`            | Extract `DEFAULT_*` from agent rocketchat.py |
| POST   | `/api/rc/kill/<name>`              | Kill the monitor process in pane 2        |
| POST   | `/api/rc/restart/<name>`           | Restart monitor in pane 2                 |

### File browser
| Method | Route                              | Action                                    |
|--------|------------------------------------|-------------------------------------------|
| GET    | `/api/browser/list?section=<s>`    | List files in a section                   |
| GET    | `/api/browser/file?section=<s>&path=<p>` | Return file content + kind          |

Sections: `docs`, `apps`, `modules`, `agents`, `archive`

### Deploy
| Method | Route           | Action                                                |
|--------|-----------------|-------------------------------------------------------|
| POST   | `/api/deploy`   | Stream `deploy.py` output via SSE                     |

---

## WebSocket — Interactive Terminal

`GET /ws/tmux/<session>` (upgraded to WebSocket via `flask-sock`)

- Forks a PTY, execs `tmux attach-session -t <session>` inside it.
- Pipes data bidirectionally: browser → PTY stdin, PTY stdout → browser.
- Sets `TERM=xterm-256color` and `COLORTERM=truecolor` so full colour works.
- Used by the **Term** button on agent cards to open a live tmux session
  in an `xterm.js` modal inside the dashboard.

---

## Dashboard UI — Key Sections

### Header
- **JARVIS v4** logo/wordmark
- Nav links: Map · Docs · Apps · Modules · Agents · Archive
- **+ Deploy Agent** → opens deploy modal with form + live streaming output
- **Tile / List view** toggle icons
- **Online only** toggle — hides offline agents
- **A–Z grid** — snaps visible/filtered cards into a sorted grid
- Clock

### Tag + Search bar (below header)
- Tag pills (one per unique tag across all agents) — click to filter
- **All** pill clears the tag filter
- Search input (right side) — real-time text filter by agent name
  - Press `/` or `Ctrl+F` anywhere to focus it
  - Both filters combine (AND logic)

### Canvas (agent map)
- Absolutely positioned, draggable agent cards
- Scrollable in both directions — floor expands as cards are dragged further
- Card positions saved to `localStorage` (`j4_positions`)
- View mode saved to `localStorage` (`j4_view`)

### Agent Cards (tile view)
Each card has:

**Left panel:**
- Status dot (green = online, grey = offline)
- Agent name + settings gear icon
- Tag chips (if tagged)
- Pane label + context % badge
- Live pane 1 preview (updated every 2.5s)
- Footer: Stop · Start · Refresh · Term buttons + dispatch count

**Right panel (apps dock):**
- RC status bubble — colour shows monitor state (green/yellow/red)
  - Click → RC config popover (channel, user, webhook URL, kill/restart buttons)
- Mail icon (only if mailinbox is configured with real credentials)
- `+` button → Add App modal

### Agent Cards (list view)
Compact single-row: dot + name + action buttons + RC icon. No preview.

### Settings Gear (per card)
Opens a centered floating modal with tabs:
- **Context** — renders `context.md` as Markdown
- **Utilities** — renders `utilities/README.md` or lists files
- **Routines** — renders `routines/README.md` or lists files
- **Tags** — add/remove tags for this agent; changes persist to `tags.json`

### File Browser Panel (nav links)
Slides in from the left. Sections:
- **Docs** — `jarvisv4/docs/*.md` and other files
- **Apps** — `jarvisv4/apps/*.py`
- **Modules** — grouped by folder with collapsible dropdowns; auto-loads `README.md`
- **Agents** — grouped by agent, collapsible; auto-loads `context.md`
- **Archive** — archived agents with Restore button per agent

### Add App Modal
Two-panel layout:
- **Left sidebar** — all apps from `APPS_REGISTRY` with colour dot + installed badge
- **Right panel** — selected app's configurable fields, save/install/remove

### Deploy Agent Modal
- Form: agent name, poll interval, RC options, optional mail inbox credentials
- Live streaming output from `deploy.py` via SSE
- Green/red status badge on completion

### Term Modal (xterm.js)
- Full interactive tmux session in the browser
- Opens on "Term" button → attaches to the agent's tmux session
- Supports copy/paste, colours, resize

---

## Credential Injection (Apps)

When an app is installed via the dashboard or via `deploy.py`:

1. The master file from `jarvisv4/apps/` is read.
2. `DEFAULT_*` constants at the top are replaced with the provided values.
3. The result is written to `agents/<name>/apps/<dest>`.

**Credential preservation on redeploy:** If `--mailinbox-*` flags are not
passed to `deploy.py`, it reads the existing constants from the agent's copy
before overwriting — so bug fixes propagate but credentials survive.

---

## Tags System

- Stored in `agents/<name>/tags.json` — simple JSON array of lowercase strings.
- Read at dashboard load and on every `fetchAgents` poll (every 5s).
- Used for: filter pills in the sub-nav, card chips, and the A-Z grid
  (which only snaps visible/unfiltered cards).

---

## File: `deploy.py`

Called by the dashboard's start/refresh/deploy routes. Key behaviour:

- Scaffolds `agents/<name>/` with `context.md` from template.
- Copies and injects `rocketchat.py` and optionally `mailinbox.py`.
- Creates the RC channel + webhook.
- Kills any existing tmux session and recreates it:
  - Pane 1: `cursor agent --yolo "read context.md"`
  - Pane 2: `python3 apps/rocketchat.py monitor`
- `--no-attach` flag skips attaching (used by dashboard).
- `--dry-run` flag prints actions without executing (safe for testing).

---

## Adding a New App (end-to-end)

1. Create `jarvisv4/apps/my-app.py` with `DEFAULT_*` stubs at the top.
2. Add to `APPS_REGISTRY` in `app.py`:
   ```python
   "myapp": {
       "label":  "My App",
       "master": "my-app.py",
       "dest":   "my-app.py",
       "color":  "#a78bfa",
       "builtin": False,
       "fields": [
           {"key": "DEFAULT_API_KEY", "label": "API Key", "secret": True},
       ],
   }
   ```
3. Restart `app.py`. The app immediately appears in every agent's Add App modal.
