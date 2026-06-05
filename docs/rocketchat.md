# Rocket.Chat integration

> **Full guide:** [rocketchat-integration.md](rocketchat-integration.md) — architecture,
> accounts, monitor polling, webhooks, multi-agent, security, troubleshooting.

Rocket.Chat is the **primary interface** between humans and JARVIS agents.

---

## 30-second summary

1. Run `python3 apps/master-rocketchat.py setup` once → `~/.config/rocketchat/config.json`
2. Run `python3 deploy.py <agent.name>` → creates `#<agent.name>` private group + webhook
3. Pane 2 polls RC every 10s; new human messages → pane 1 (Cursor agent)
4. Agent replies: `python3 apps/rocketchat.py send "…"` from its agent directory

---

## Quick setup

```bash
python3 apps/master-rocketchat.py setup
python3 apps/master-rocketchat.py test
python3 deploy.py example.com --no-attach
```

Post in `#example.com` in Rocket.Chat. Watch:

```bash
tail -f agents/example.com/logs/dispatch.log
```

---

## Key files

| File | Role |
|------|------|
| `apps/master-rocketchat.py` | Template + supervisor admin CLI |
| `agents/<n>/apps/rocketchat.py` | Per-agent monitor + send (injected config) |
| `~/.config/rocketchat/config.json` | Shared RC credentials |

---

## Read next

- [rocketchat-integration.md](rocketchat-integration.md) — complete integration guide
- [macbook-tmux-setup.md](macbook-tmux-setup.md) — tmux + MacBook workflow
- [deployment-guide.md](deployment-guide.md) — full deploy walkthrough
- [stop-signal.md](stop-signal.md) — STOP / HALT / ABORT
