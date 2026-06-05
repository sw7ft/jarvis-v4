# Deployment guide

> **Documentation hub:** [docs/README.md](README.md) · **Security:** [../SECURITY.md](../SECURITY.md)

This is the **complete walkthrough** for taking JARVIS v4 from a fresh clone
to a working multi-agent setup on your own machine. Read this once end-to-end,
then use the linked deep-dives for Rocket.Chat and MacBook/tmux specifics.

**Time estimate:** 30–45 minutes for first agent (including RC server setup if
you do not already have one).

---

## What you are building

```
Your MacBook (or Linux box)
├── JARVIS repo          deploy.py + app.py + master apps
├── tmux sessions        one per agent (Cursor + RC monitor)
├── agents/<name>/       isolated working dirs on disk
└── Dashboard :5112      optional web UI for the fleet

Rocket.Chat server       (self-hosted or cloud)
├── #agent-one           private group ↔ tmux session agent-one
├── #agent-two           private group ↔ tmux session agent-two
└── webhooks             contact forms, external integrations
```

Humans talk to agents **only in Rocket.Chat**. Agents work in **Cursor CLI**
inside **tmux**, and post replies back to the same channel.

---

## Prerequisites checklist

Before you start, confirm you have:

| Requirement | Verify |
|-------------|--------|
| macOS 12+ or Linux | `uname -a` |
| Python 3.9+ | `python3 --version` |
| git | `git --version` |
| tmux 3+ | `tmux -V` |
| Cursor CLI | `cursor agent --version` |
| Rocket.Chat server | Browser login works |
| RC admin account | Can create private groups |
| RC bot account | Can post in channels (can be same as admin for small setups) |

Install gaps on Mac:

```bash
brew install tmux jq
pip install -r requirements.txt
```

See [macbook-tmux-setup.md](macbook-tmux-setup.md) for Mac-specific details.

---

## Step 1 — Clone and install

```bash
git clone <your-repo-url> jarvisv4
cd jarvisv4

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional later: `pip install playwright` (Browser app only).

---

## Step 2 — Rocket.Chat credentials (one time)

JARVIS stores shared RC credentials in one place:

```bash
python3 apps/master-rocketchat.py setup
```

You will be asked for:

| Field | Used for |
|-------|----------|
| Server URL | e.g. `https://chat.yourdomain.com` |
| Admin username/password | Create private groups + register webhooks at deploy |
| Bot username/password | Monitor polling + agent replies (can match admin) |

Output: `~/.config/rocketchat/config.json` (mode **600** — never commit).

Verify:

```bash
python3 apps/master-rocketchat.py channels
python3 apps/master-rocketchat.py users
python3 apps/master-rocketchat.py test
```

**Deep dive:** [rocketchat-integration.md](rocketchat-integration.md)

---

## Step 3 — Pick an agent name

The agent name is the **single identifier** used everywhere:

| Agent name | RC channel | tmux session | Agent directory |
|------------|------------|--------------|-----------------|
| `example.com` | `#example.com` | `example-com` | `agents/example.com/` |
| `my-project` | `#my-project` | `my-project` | `agents/my-project/` |

Rules: starts with a letter or number; then letters, numbers, `.`, `-`, `_`.

Dots are allowed in the name but **tmux replaces them with dashes** for the
session name (tmux uses `.` as a pane separator).

---

## Step 4 — Deploy the agent

```bash
python3 deploy.py example.com --no-attach
```

Use `--no-attach` so deploy returns to your shell instead of jumping into tmux.
Omit it if you want to land inside the session immediately.

### What deploy does (automatically)

1. Creates `agents/example.com/` with `context.md`, sandbox files, empty logs
2. Creates RC **private group** `#example.com` (admin + bot as members)
3. Registers an **incoming webhook** for that channel
4. Copies `apps/master-rocketchat.py` → `agents/example.com/apps/rocketchat.py`
   with channel, webhook URL, tmux session, and interval **injected at the top**
5. Kills any existing tmux session `example-com`
6. Starts new tmux session:
   - **Pane 1:** `cursor agent --sandbox enabled --model composer-2.5 "read context.md"`
   - **Pane 2:** `python3 apps/rocketchat.py monitor "#example.com" …`

Dry run (no writes, no tmux):

```bash
python3 deploy.py example.com --dry-run
```

Scaffold files only (no tmux kill/create):

```bash
python3 deploy.py example.com --no-launch
```

---

## Step 5 — Verify tmux

```bash
tmux ls
# example-com: 1 windows (created …)

tmux attach -t example-com
```

Layout:

```
┌─────────────────────────────────────┐
│  Pane 1 — Cursor agent              │  ← AI worker
│  (booting: read context.md)         │
├─────────────────────────────────────┤
│  Pane 2 — RC monitor + shell        │  ← polls Rocket.Chat
└─────────────────────────────────────┘
```

Detach without stopping: **`Ctrl-b` then `d`**

Pane numbers are **1** and **2** (deploy sets `base-index 1`).

---

## Step 6 — Test Rocket.Chat

1. Open Rocket.Chat in browser or desktop app
2. Join private group **`#example.com`**
3. Post: `Hello — what agent are you?`
4. Within ~10 seconds (default poll interval):
   - ⏳ hourglass reaction appears on your message
   - Pane 1 receives the dispatch
   - Agent replies in channel via `rocketchat.py send`

Watch logs:

```bash
tail -f agents/example.com/logs/dispatch.log
tail -f agents/example.com/logs/monitor.log
```

Expected dispatch log lines:

```json
{"event":"dispatch","channel":"#example.com","sender":"you","text":"Hello …"}
{"event":"send","channel":"#example.com","text":"…agent reply…"}
```

---

## Step 7 — Start the dashboard (optional but recommended)

```bash
python3 app.py
# → http://localhost:5112
```

From the dashboard you can:

- See all agents on a draggable map (online/offline, RC health)
- Deploy new agents with a form + live log stream
- Restart / stop tmux sessions
- Open a **live terminal** in the browser (xterm.js → tmux attach)
- Install optional apps (Mail, Browser) per agent

Keep it running in a separate terminal tab, or use tmux for that too.

---

## Step 8 — Customize the agent

Edit `agents/example.com/context.md`:

- Client name, SSH host, website paths
- What the agent should and should not do
- Links to docs in `agents/example.com/docs/`

The agent reads this on boot. Append findings as work progresses.

Compare structure with `agents/_example/`.

---

## Step 9 — (Optional) SSH for remote servers

If the agent manages a remote machine, add to `~/.ssh/config`:

```
Host example.com
    HostName 203.0.113.10
    User deploy
    IdentityFile ~/.ssh/id_ed25519
```

The Host alias **must match** the agent name. Agent runs `ssh example.com` from
its sandboxed working directory.

---

## Step 10 — Deploy more agents

Each additional client is one command:

```bash
python3 deploy.py client-two.com --no-attach
python3 deploy.py internal-tools --no-attach
```

Each gets:

- Its own `agents/<name>/` directory
- Its own `#<name>` RC channel
- Its own tmux session
- Its own Cursor process (RAM scales linearly — see MacBook guide)

List sessions:

```bash
tmux ls
```

---

## Optional apps (install per agent)

Full architecture: **[apps-system.md](apps-system.md)**. Remote site modules
(contact forms): **[modules-system.md](modules-system.md)**.

### Mail inbox

```bash
python3 deploy.py example.com \
  --mailinbox-host mail.example.com \
  --mailinbox-email agent@example.com \
  --mailinbox-password 'secret' \
  --no-attach
```

Or: dashboard → agent card → **+** → Mail Inbox → Install.

See [mailinbox.md](mailinbox.md).

### Browser (Playwright + Chrome)

Dashboard → **+** → Browser → **Install** (port and profile auto-filled).

See [browser.md](browser.md).

---

## Day-2 operations

| Task | How |
|------|-----|
| Redeploy (refresh RC copy, restart tmux) | `python3 deploy.py <name> --no-attach` or dashboard **Refresh** |
| Restart RC monitor only | Dashboard RC popover → **Restart Monitor** |
| Stop agent | Dashboard **Stop** or `tmux kill-session -t <session>` |
| Abort agent mid-task | Post `STOP` in the agent's RC channel |
| Change model | Write slug to `agents/<name>/.cursor-model`, then Refresh |
| View audit trail | `tail -f agents/<name>/logs/dispatch.log` |

---

## Production tips

1. **Gitignore agent secrets** — default `.gitignore` excludes `agents/*/apps/` and logs
2. **Do not expose dashboard** without auth — bind to localhost or reverse proxy
3. **Restart monitors** after upgrading `master-rocketchat.py`
4. **Laptop sleep** — tmux survives; RC monitor resumes polling on wake; Cursor may need Refresh after long sleep
5. **Back up** `~/.config/rocketchat/config.json` securely

---

## Troubleshooting

See [troubleshooting.md](troubleshooting.md).

Quick RC checks:

```bash
python3 apps/master-rocketchat.py test
pgrep -af "rocketchat.py monitor"
tmux capture-pane -t example-com:main.1 -p | tail -20
```

---

## Next reads

| Doc | Why |
|-----|-----|
| [rocketchat-integration.md](rocketchat-integration.md) | How RC wiring works in depth |
| [macbook-tmux-setup.md](macbook-tmux-setup.md) | Running the fleet on a MacBook |
| [architecture.md](architecture.md) | System design |
| [agents.md](agents.md) | Agent directory reference |
