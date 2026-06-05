# Dashboard API reference

The dashboard (`app.py`) exposes REST JSON APIs and one WebSocket route.
Base URL: `http://localhost:5112` (default).

This is a summary — see [app-context.md](app-context.md) for UI integration details.

---

## Agents

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/agents` | List all agents (online, RC status, tags, apps) |
| POST | `/api/agent/stop` | Kill tmux session `{name}` |
| POST | `/api/agent/start` | Run deploy.py `--no-attach` |
| POST | `/api/agent/refresh` | Stop + start |
| POST | `/api/agent/archive` | Move to archive/ |
| POST | `/api/agent/restore` | Restore from archive/ |
| GET | `/api/agent/tags/<name>` | Read tags.json |
| POST | `/api/agent/tags/<name>` | Write tags |
| GET | `/api/agent/model/<name>` | Get model slug + choices |
| POST | `/api/agent/model/<name>` | Set `.cursor-model` |
| GET | `/api/pane/snapshots` | Pane 1 preview text for cards |

---

## Apps

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/apps/registry` | APPS_REGISTRY (labels, fields) |
| GET | `/api/apps/installed/<name>` | Which apps exist for agent |
| GET | `/api/apps/config/<name>/<app_id>` | Read DEFAULT_* values |
| POST | `/api/apps/install` | Copy master + inject `{agent, app_id, fields}` |
| POST | `/api/apps/remove` | Delete app file |

Built-in apps: `rocketchat`. Optional: `mailinbox`, `browser`.

---

## Rocket.Chat monitor

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/rc/config/<name>` | RC constants from agent rocketchat.py |
| POST | `/api/rc/kill/<name>` | Kill monitor in pane 2 |
| POST | `/api/rc/restart/<name>` | Restart monitor |
| GET | `/api/rocketchat/feed` | Global RC message feed (dashboard viewer) |

---

## Mail / Browser

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/mailinbox/config/<name>` | Mail config |
| POST | `/api/mailinbox/test` | Run mailinbox.py test |
| GET | `/api/browser/config/<name>` | Browser config + live status |
| POST | `/api/browser/launch` | Start Chrome |
| POST | `/api/browser/stop` | Stop Chrome |
| POST | `/api/browser/test` | CDP test |
| POST | `/api/browser/goto` | Navigate `{name, url}` |
| GET | `/api/browser/screenshot/<name>` | PNG screenshot |

---

## Deploy

| Method | Route | Description |
|--------|-------|-------------|
| POST | `/api/deploy` | SSE stream of deploy.py output `{name, ...}` |

---

## File browser

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/browser/list?section=` | List docs/apps/modules/agents/archive |
| GET | `/api/browser/file?section=&path=` | Read file content |

Note: `/api/browser/*` here is the **file browser**, not the Browser app.

---

## WebSocket

| Route | Description |
|-------|-------------|
| `/ws/tmux/<session>` | Interactive tmux PTY (xterm.js in dashboard) |

See [xterm.md](xterm.md).

---

## Response conventions

- JSON errors: `{"error": "message"}` with 4xx/5xx
- App install: `{"ok": true, "message": "..."}`
- SSE deploy: `data: "<line>"` lines, final `{"__exit__": 0}`

---

## Authentication

Default install: **no auth** on port 5112. Bind to localhost or put behind
reverse proxy with auth for production exposure.
