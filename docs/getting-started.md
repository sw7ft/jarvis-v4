# Getting started

> **Prefer a checklist?** See [deployment-guide.md](deployment-guide.md).
> **All docs:** [README.md](README.md)

This guide walks through a clean install of JARVIS v4 from this open-source
tree — from zero to a working agent replying in Rocket.Chat.

---

## 1. Clone and install Python deps

```bash
git clone <repo-url> jarvisv4
cd jarvisv4
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optional (Browser app only):

```bash
pip install playwright
# Uses system Google Chrome — no `playwright install chromium` required
```

---

## 2. Install system dependencies

| Tool | Purpose | Install |
|------|---------|---------|
| **tmux** | Agent sessions | `brew install tmux` / `apt install tmux` |
| **Cursor CLI** | Pane 1 agent | [cursor.com](https://cursor.com) — verify: `cursor agent --version` |
| **jq** | Log parsing (optional) | `brew install jq` |

---

## 3. Configure Rocket.Chat

You need an RC server with:

- Admin account (for channel + webhook creation)
- Bot/service account (for posting replies)

Run the setup wizard once:

```bash
python3 apps/master-rocketchat.py setup
```

This writes `~/.config/rocketchat/config.json` (mode 600) with:

- Server URL
- Admin credentials (channel/webhook management)
- Bot credentials (monitor + send)

Verify:

```bash
python3 apps/master-rocketchat.py channels
python3 apps/master-rocketchat.py users
```

See [rocketchat.md](rocketchat.md) for RC server requirements and channel
conventions.

---

## 4. (Optional) SSH host for the agent

If your agent will manage a remote server, add an SSH config entry:

```
Host example.com
    HostName 203.0.113.10
    User deploy
    IdentityFile ~/.ssh/example_deploy
```

The agent name should match the Host alias: `example.com`.

JARVIS works without SSH — agents can still manage local files and chat.

---

## 5. Deploy your first agent

Pick an agent name (alphanumeric, dots, dashes):

```bash
python3 deploy.py example.com
```

What happens:

1. Creates `agents/example.com/` with `context.md`, sandbox files, empty logs
2. Creates private RC group `#example.com` + incoming webhook
3. Writes `agents/example.com/apps/rocketchat.py` with injected config
4. Kills any existing tmux session `example-com`
5. Launches tmux:
   - Pane 1: `cursor agent … "read context.md"`
   - Pane 2: RC monitor polling `#example.com`
6. Attaches to tmux (use `--no-attach` to skip)

Dry run (no changes):

```bash
python3 deploy.py example.com --dry-run
```

Scaffold only (no tmux restart):

```bash
python3 deploy.py example.com --no-launch
```

---

## 6. Test the agent

1. Open Rocket.Chat → join `#example.com`
2. Post: `Hello, what is your agent name?`
3. Within ~10 seconds (poll interval), pane 2 dispatches to pane 1
4. Agent should reply in channel via `rocketchat.py send`

Watch logs:

```bash
tail -f agents/example.com/logs/dispatch.log
tail -f agents/example.com/logs/monitor.log
```

Attach to tmux:

```bash
tmux attach -t example-com
# Pane 1 = agent, Pane 2 = monitor + shell
```

---

## 7. Start the dashboard

```bash
python3 app.py
# Open http://localhost:5112
```

From the dashboard you can:

- See all agents on the map
- Deploy new agents
- Restart / stop sessions
- Install Mail or Browser apps per agent
- Open live tmux terminal in browser

---

## 8. Optional apps

### Mail inbox

```bash
python3 deploy.py example.com \
  --mailinbox-host mail.example.com \
  --mailinbox-email agent@example.com \
  --mailinbox-password 'secret'
```

Or install later via dashboard → agent card → **+** → Mail Inbox.

See [mailinbox.md](mailinbox.md).

### Browser

Dashboard → **+** → Browser → **Install** (defaults auto-filled from agent name).

See [browser.md](browser.md).

---

## 9. Customize agent context

Edit `agents/example.com/context.md` with client-specific instructions:

- SSH details, website paths, contacts, procedures
- What the agent should / should not do

Agents read this on boot and you can append findings as they work.

Compare with `agents/_example/context.md` for the template shape.

---

## 10. Production checklist

- [ ] RC credentials in `~/.config/rocketchat/config.json` (600)
- [ ] SSH keys and `~/.ssh/config` entries per agent
- [ ] `agents/*/apps/` and `agents/*/logs/` gitignored (default in `.gitignore`)
- [ ] Dashboard bound to localhost or behind auth if exposed
- [ ] Monitor restarted after upgrading `master-rocketchat.py`
- [ ] Model slug set (`composer-2.5` default) — override via `agents/<name>/.cursor-model`

---

## Next steps

- [architecture.md](architecture.md) — deep dive
- [deploy-and-apps.md](deploy-and-apps.md) — deploy flags and redeploy behavior
- [troubleshooting.md](troubleshooting.md) — when things break
- [modules.md](modules.md) — ship website contact forms, etc.
