# MacBook + tmux setup

JARVIS v4 is designed to run on a **developer MacBook** as the control plane:
Rocket.Chat monitors, Cursor agents, optional Chrome profiles, and the dashboard
all live on one machine. **tmux** keeps every agent in a persistent session you
can attach to, detach from, and survive terminal closes — without background
daemon complexity.

This guide is Mac-specific. Linux works the same except install commands.

---

## Why tmux on a MacBook?

| Without tmux | With tmux |
|--------------|-----------|
| Close Terminal → kill agent | Close Terminal → agents keep running |
| One agent per window | Many agents, `tmux ls` to list |
| Hard to automate pane layout | deploy.py creates consistent 2-pane layout |
| No remote peek | `ssh macbook` + attach from elsewhere |

JARVIS uses tmux as the **process supervisor** for each agent — not systemd, not
Docker. One tmux session = one agent = one RC channel.

---

## Install dependencies (Mac)

### Homebrew (if needed)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### Required tools

```bash
brew install tmux jq git python@3.12
```

### Python venv (in repo)

```bash
cd ~/jarvisv4
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Add to `~/.zshrc` (optional):

```bash
alias j4='cd ~/jarvisv4 && source .venv/bin/activate'
```

### Cursor CLI

Install [Cursor](https://cursor.com) desktop, then ensure CLI is in PATH:

```bash
cursor agent --version
```

If missing, install shell command from Cursor → Command Palette → “Install cursor command”.

### Verify stack

```bash
tmux -V          # tmux 3.x
python3 --version
cursor agent --list-models | head -5
```

---

## How deploy lays out tmux

For agent `example.com`, deploy creates session **`example-com`**:

```bash
python3 deploy.py example.com --no-attach
```

Commands run internally:

```bash
tmux new-session -d -s example-com -n main
tmux set-option -t example-com base-index 1
tmux set-option -t example-com pane-base-index 1
# Pane 1: Cursor agent
tmux send-keys -t example-com:main \
  'cd …/agents/example.com && cursor agent --yolo --sandbox enabled --model composer-2.5 "read context.md"' Enter
# Split horizontally
tmux split-window -t example-com:main -v
# Pane 2: RC monitor (background) + interactive shell
tmux send-keys -t example-com:main.2 '… monitor … & trap …; $SHELL' Enter
```

Visual (pane 1 top, pane 2 bottom):

```
┌─ tmux session: example-com ─────────────────────────────┐
│ main window                                              │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ .1  Cursor agent — pane 1                           │ │
│ │     Working dir: agents/example.com/                │ │
│ │     Boot: read context.md → wait for dispatches     │ │
│ ├─────────────────────────────────────────────────────┤ │
│ │ .2  Monitor PID: 12345 — log: …/monitor.log        │ │
│ │     (python monitor running in background)          │ │
│ │     zsh prompt — you can run commands here too      │ │
│ └─────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

Pane indices are **1** and **2** (not 0/1) — deploy sets `pane-base-index 1`.

---

## Essential tmux commands

Run these from **any** terminal on the Mac:

| Action | Command |
|--------|---------|
| List sessions | `tmux ls` |
| Attach | `tmux attach -t example-com` |
| Detach (agents keep running) | `Ctrl-b` then `d` |
| Switch panes | `Ctrl-b` then `↑` / `↓` |
| Scroll pane history | `Ctrl-b` then `[`, arrow keys, `q` to exit |
| Kill session (stops agent) | `tmux kill-session -t example-com` |
| Rename session | `Ctrl-b` then `$` |

Target syntax: `example-com:main.1` = session `example-com`, window `main`, pane `1`.

---

## Typical MacBook workflow

### Morning — start dashboard + check fleet

```bash
cd ~/jarvisv4 && source .venv/bin/activate
python3 app.py &
open http://localhost:5112
tmux ls
```

### Deploy a new client agent

```bash
python3 deploy.py newclient.com --no-attach
# Join #newclient.com in Rocket.Chat desktop app
```

### Peek at what an agent is doing

```bash
tmux attach -t newclient-com
# Watch pane 1 (Cursor output)
# Ctrl-b d to detach
```

Or use dashboard **Term** button — browser xterm attaches to the same session.

### Laptop sleep / close lid

- **tmux sessions survive** — processes keep running
- **RC monitor** resumes polling on wake (may miss one interval)
- **Cursor** may need **Refresh** on dashboard if stuck after long sleep
- **Wi-Fi drop** — monitor retries on next poll; no special action

### End of day

Leave tmux running (agents stay online in RC) or stop selectively:

```bash
tmux kill-session -t example-com   # one agent
# or dashboard Stop button
```

---

## Running multiple agents on one MacBook

### RAM and CPU

Each agent ≈ one Cursor CLI process + one Python monitor + shell overhead.

| Agents | Rough guidance |
|--------|----------------|
| 1–3 | Comfortable on 16 GB MacBook |
| 5–10 | 32 GB recommended; watch Activity Monitor |
| 10+ | Consider dedicated Mac mini / server |

Use dashboard **Online only** filter and stop idle agents.

### Naming many sessions

```bash
tmux ls
# acme-com: 1 windows
# example-com: 1 windows
# internal-tools: 1 windows
```

Session name = agent name with `.` → `-`.

### Organize terminal work

**Option A — one Terminal tab per attach**

Tab 1: dashboard logs  
Tab 2: `tmux attach -t client-a-com`  
Tab 3: `tmux attach -t client-b-com`

**Option B — tmux nesting (advanced)**

Attach to outer tmux only if you know what you're doing — JARVIS sessions are
top-level, not nested.

**Option C — dashboard only**

Use map + Term modal — never attach locally.

---

## Rocket.Chat desktop on Mac

Install Rocket.Chat from App Store or https://www.rocket.chat/apps

Add your server URL once. Join each `#agent-name` private group you care about.
Desktop notifications → you see agent replies without watching tmux.

Mobile app works the same — ops from phone, agents still run on MacBook.

---

## SSH from agents to remote servers

Agents SSH **out** from the MacBook to client servers:

```
~/.ssh/config on MacBook:

Host example.com
    HostName 203.0.113.10
    User deploy
    IdentityFile ~/.ssh/example_deploy
```

Agent runs `ssh example.com` from `agents/example.com/`. Keys live on the
MacBook, not in the repo.

Test before deploy:

```bash
ssh example.com hostname
```

---

## Optional: run dashboard on login

Create `~/Library/LaunchAgents/com.jarvis.dashboard.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jarvis.dashboard</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/jarvisv4/.venv/bin/python3</string>
    <string>/Users/YOU/jarvisv4/app.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/jarvis-dashboard.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/jarvis-dashboard.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.jarvis.dashboard.plist
```

Replace `YOU` with your username. Dashboard stays on http://localhost:5112.

---

## Optional: tmux config (~/.tmux.conf)

Quality-of-life on Mac:

```bash
# ~/.tmux.conf
set -g mouse on
set -g history-limit 50000
set -g default-terminal "tmux-256color"
bind-key | split-window -h
bind-key - split-window -v
```

Reload: `tmux source-file ~/.tmux.conf`

---

## File locations on Mac

| Path | Contents |
|------|----------|
| `~/jarvisv4/` | Repo |
| `~/jarvisv4/agents/<name>/` | Per-agent state |
| `~/.config/rocketchat/config.json` | RC credentials (600) |
| `/tmp/jarvis-app.log` | Dashboard stdout if backgrounded |

---

## Full first-time Mac setup (copy-paste)

```bash
# 1. Tools
brew install tmux jq python@3.12

# 2. Repo
git clone <repo-url> ~/jarvisv4
cd ~/jarvisv4
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Rocket.Chat
python3 apps/master-rocketchat.py setup
python3 apps/master-rocketchat.py test

# 4. First agent
python3 deploy.py example.com --no-attach

# 5. Verify
tmux ls
tmux attach -t example-com    # Ctrl-b d to detach

# 6. Dashboard
python3 app.py
open http://localhost:5112

# 7. Test in Rocket.Chat app — post in #example.com
tail -f agents/example.com/logs/dispatch.log
```

---

## Related docs

- [deployment-guide.md](deployment-guide.md) — full deploy walkthrough
- [rocketchat-integration.md](rocketchat-integration.md) — RC wiring in depth
- [xterm.md](xterm.md) — in-browser terminal via dashboard
- [troubleshooting.md](troubleshooting.md) — when things break
