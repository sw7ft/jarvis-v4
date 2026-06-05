# Architecture

> **Related:** [deployment-guide.md](deployment-guide.md) · [rocketchat-integration.md](rocketchat-integration.md) · [apps-system.md](apps-system.md) · [docs index](README.md)

JARVIS v4 is a **local-first multi-agent control plane**. Each agent is an
isolated unit: one Rocket.Chat channel, one tmux session, one working
directory, one Cursor CLI process. A central dashboard and deploy tool manage
the fleet; agents never share state.

---

## High-level diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Human operators                                                        │
│  Rocket.Chat channels  ·  Dashboard (app.py :5112)                     │
└───────────────┬───────────────────────────────┬─────────────────────────┘
                │                               │
                ▼                               ▼
┌───────────────────────────┐     ┌───────────────────────────────────────┐
│  Rocket.Chat server       │     │  JARVIS host (your Mac / Linux box)   │
│  - private groups #agent  │     │                                       │
│  - incoming webhooks      │     │  deploy.py ──► scaffold + tmux      │
└───────────────┬───────────┘     │  app.py    ──► monitor / manage      │
                │                 │                                       │
                │ poll + webhook  │  agents/<name>/                       │
                ▼                 │    ├── context.md                     │
┌───────────────────────────┐     │    ├── apps/rocketchat.py (injected)  │
│  Per-agent tmux session   │◄────│    ├── apps/mailinbox.py (optional)   │
│  <name-with-dashes>       │     │    ├── apps/browser.py (optional)     │
│                           │     │    └── logs/dispatch.log              │
│  Pane 1: cursor agent     │     └───────────────────────────────────────┘
│  Pane 2: RC monitor       │
└───────────────┬───────────┘
                │ SSH (optional)
                ▼
┌───────────────────────────┐
│  Remote server            │
│  ssh <agent.name>         │
│  (website, cron, etc.)    │
└───────────────────────────┘
```

---

## The 1:1:1:1 mapping

Every agent name (`example.com`) maps consistently:

| Resource | Value |
|----------|-------|
| Agent directory | `agents/example.com/` |
| Rocket.Chat channel | `#example.com` (private group) |
| tmux session | `example-com` (dots → dashes) |
| SSH host alias | `example.com` in `~/.ssh/config` |

This removes ambiguity: one name everywhere except tmux, where dots are illegal
separators in `session:window.pane` syntax.

---

## Runtime: two-pane tmux model

`deploy.py` creates one tmux window (`main`) with two panes:

| Pane | Process | Role |
|------|---------|------|
| **1** | `cursor agent --sandbox enabled --model … "read context.md"` | The AI worker. Reads `context.md`, uses tools, SSH, apps. |
| **2** | `python3 apps/rocketchat.py monitor …` | Polls RC every N seconds. Forwards human messages to pane 1. |

**Receive flow**

1. Human posts in `#example.com`
2. Pane 2 monitor sees new message (poll interval, default 10s)
3. Monitor builds a dispatch prompt and `tmux send-keys` into pane 1
4. Cursor agent processes message, does work, replies via `rocketchat.py send`

**Send flow**

Agent runs from its directory:

```bash
python3 apps/rocketchat.py send "#example.com" "Hello from the agent"
```

Channel can be omitted when baked into `DEFAULT_CHANNEL` at deploy time.

**STOP flow**

User posts `STOP` (standalone word) → monitor sends `Ctrl-C` to pane 1,
posts `⛔ Stopped.`, logs `event: stop`. See [stop-signal.md](stop-signal.md).

---

## Master vs per-agent copies

| File | Location | Used by |
|------|----------|---------|
| `apps/master-rocketchat.py` | Repo root | Supervisor admin CLI + template |
| `agents/<name>/apps/rocketchat.py` | Per agent | That agent's monitor + send |

`deploy.py` copies master → agent and **injects** `DEFAULT_*` constants at
the top (channel, webhook URL, tmux session, interval, persona). Agents never
read config files at runtime for RC — everything is in the script header.

Same pattern for optional apps (`mailinbox.py`, `browser.py`).

---

## Dashboard (`app.py`)

Single-file Flask app (~12k lines) with inline HTML/CSS/JS:

- **Agent map** — draggable cards, online/offline, RC health, app icons
- **Deploy modal** — streams `deploy.py` output via SSE
- **App manager** — install/remove/configure apps per agent (`APPS_REGISTRY`)
- **Live terminal** — WebSocket PTY bridge to tmux (`/ws/tmux/<session>`)
- **File browser** — docs, apps, modules, agent context

No build step. Run: `python3 app.py` → port **5112**.

Details: [app-context.md](app-context.md), [api-reference.md](api-reference.md).

---

## Credential injection

When an app is installed or an agent is deployed:

1. Read master file from `apps/<master>.py`
2. Replace `DEFAULT_*` lines at top with provided values
3. Write to `agents/<name>/apps/<dest>.py`, chmod 755

**Preservation on redeploy:** mail credentials are rescued from the existing
agent copy if `--mailinbox-*` flags are omitted. Browser defaults are derived
from agent name (port hash, profile path). RC webhook is reused if one exists.

---

## Isolation & sandbox

Each agent gets Cursor sandbox files at deploy:

- `agents/<name>/.cursor/sandbox.json` — filesystem allow/deny paths
- `agents/<name>/.cursor/rules/sandbox.mdc` — scope rules for the model

Agents are instructed to stay inside `agents/<name>/`. Dispatch prompts include
a scope fence. See [sandbox.md](sandbox.md).

---

## Modules

Optional packages in `modules/<name>/` extend agents without changing
`deploy.py`. Each module ships a `MODULE.md` with exact deploy steps. Example:
`contact-form` — PHP handler posting to RC webhook.

See [modules.md](modules.md).

---

## Dispatch log (audit trail)

JSON Lines at `agents/<name>/logs/dispatch.log`:

| Event | Meaning |
|-------|---------|
| `dispatch` | Monitor forwarded inbound RC message to pane 1 |
| `send` | Agent called `rocketchat.py send` |
| `stop` | User sent STOP; Ctrl-C delivered |

Useful for debugging “did the agent see my message?” and replay after restarts.

---

## Design principles

1. **Kill session = kill everything** — tmux death stops both panes cleanly
2. **No shared agent state** — each client is a separate directory + channel
3. **Config in code headers** — per-agent apps are self-contained scripts
4. **Dashboard never replaces deploy** — deploy scaffolds; dashboard manages live fleet
5. **Human-in-the-loop via chat** — RC is the primary interface; dashboard is ops

---

## What runs where

| Component | Typical host |
|-----------|--------------|
| `deploy.py`, `app.py`, agents, tmux | Your JARVIS machine (Mac/Linux) |
| Rocket.Chat | Self-hosted or cloud |
| Cursor CLI | Same machine as tmux pane 1 |
| Chrome (browser app) | Same machine as agents (local profiles) |
| Client websites / SSH targets | Remote servers per agent |

Browser and mail apps connect **outbound** from the JARVIS host — no inbound
ports required beyond the dashboard (5112, local only by default).
