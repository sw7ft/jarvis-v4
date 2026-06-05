# JARVIS v4

**Multi-agent AI operations on your MacBook** — each client gets a Rocket.Chat
channel, a persistent tmux session, and a Cursor CLI agent. Operators chat in
Rocket.Chat; agents work locally and reply in the same room.

> **Keywords:** Rocket.Chat · tmux · Cursor agent · multi-tenant ops · self-hosted · MacBook control plane

---

## Documentation

| Start here | Description |
|------------|-------------|
| **[📖 Full documentation index](docs/README.md)** | Every guide, searchable by topic |
| **[🚀 Deployment guide](docs/deployment-guide.md)** | Clone → RC → deploy → test (recommended first read) |
| **[💬 Rocket.Chat integration](docs/rocketchat-integration.md)** | Channels, monitor, webhooks, security |
| **[💻 MacBook + tmux setup](docs/macbook-tmux-setup.md)** | Sessions, attach/detach, multi-agent |
| **[🧩 Agent apps](docs/apps-system.md)** | RC, mail, browser — install & injection |
| **[📦 Modules](docs/modules-system.md)** | Contact forms & remote deploy packages |

**Security:** [SECURITY.md](SECURITY.md) · run `./scripts/audit-secrets.sh` before pushing

---

## What is this?

```
Operator (phone/desktop)  →  Rocket.Chat #client.com
                                    ↓ poll
MacBook: tmux session client-com
         ├── pane 1: Cursor agent (does the work)
         └── pane 2: rocketchat.py monitor (bridge)
                                    ↓
Agent replies  →  rocketchat.py send  →  #client.com
```

One laptop runs **many agents** — each isolated in its own directory, channel,
and tmux session. Optional web dashboard at `http://localhost:5112`.

---

## Quick start (Mac, ~5 minutes)

```bash
brew install tmux jq
git clone https://github.com/sw7ft/jarvis-v4.git && cd jarvis-v4

./scripts/check-prerequisites.sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 apps/master-rocketchat.py setup    # RC credentials → ~/.config/rocketchat/
python3 deploy.py example.com --no-attach  # channel #example.com + tmux session

python3 app.py                             # dashboard → http://localhost:5112
```

Post in `#example.com` in Rocket.Chat. The agent should reply within ~10 seconds.

**Step-by-step:** [docs/deployment-guide.md](docs/deployment-guide.md)

---

## Repository layout

| Path | Purpose |
|------|---------|
| [`deploy.py`](deploy.py) | Create/refresh agents, RC channel + webhook, tmux |
| [`app.py`](app.py) | Web dashboard (Flask, port 5112) |
| [`apps/`](apps/) | Master app scripts (copied + injected per agent) |
| [`modules/`](modules/) | Optional remote deploy packages (e.g. contact-form PHP) |
| [`docs/`](docs/) | **All documentation** — start at [docs/README.md](docs/README.md) |
| [`agents/_example/`](agents/_example/) | Reference agent scaffold (no secrets) |
| [`MASTER-CONTEXT.md`](MASTER-CONTEXT.md) | Rules every agent reads at runtime |

---

## Requirements

- macOS 12+ or Linux
- Python 3.9+, tmux 3+, [Cursor CLI](https://cursor.com)
- Rocket.Chat server (self-hosted or cloud)
- Optional: Google Chrome (browser app), IMAP server (mail app)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Do not commit credentials, real agent
directories, or production hostnames. PRs should pass `./scripts/audit-secrets.sh`.

---

## License

MIT — [LICENSE](LICENSE)
