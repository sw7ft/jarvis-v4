# JARVIS v4 — Documentation

Complete documentation for deploying and operating JARVIS v4: multi-agent AI
ops with Rocket.Chat, tmux, and Cursor CLI on a MacBook (or Linux host).

**New here?** Read in this order:

1. [deployment-guide.md](deployment-guide.md) — end-to-end setup
2. [rocketchat-integration.md](rocketchat-integration.md) — how chat connects to agents
3. [macbook-tmux-setup.md](macbook-tmux-setup.md) — day-to-day operations

---

## Table of contents

### Getting started

| Document | What you'll learn |
|----------|-------------------|
| [deployment-guide.md](deployment-guide.md) | Clone, install, RC setup, first agent, dashboard |
| [getting-started.md](getting-started.md) | Alternate step-by-step first agent |
| [macbook-tmux-setup.md](macbook-tmux-setup.md) | tmux layout, attach/detach, RAM, LaunchAgent |
| [configuration.md](configuration.md) | Models, paths, env vars, CLI flags |
| [architecture.md](architecture.md) | System design and data flow |

### Rocket.Chat

| Document | What you'll learn |
|----------|-------------------|
| [rocketchat-integration.md](rocketchat-integration.md) | **Full RC guide** — accounts, monitor, webhooks, multi-agent |
| [rocketchat.md](rocketchat.md) | Quick RC command reference |
| [stop-signal.md](stop-signal.md) | Abort agents with STOP / HALT / ABORT |

### Apps (local CLI tools per agent)

| Document | What you'll learn |
|----------|-------------------|
| [apps-system.md](apps-system.md) | **Full apps guide** — registry, injection, RC/mail/browser |
| [deploy-and-apps.md](deploy-and-apps.md) | `deploy.py` + apps quick reference |
| [mailinbox.md](mailinbox.md) | IMAP/SMTP email CLI |
| [browser.md](browser.md) | Playwright Chrome + site context files |

### Modules (remote deploy packages)

| Document | What you'll learn |
|----------|-------------------|
| [modules-system.md](modules-system.md) | **Full modules guide** — contact-form, webhooks, authoring |
| [modules.md](modules.md) | Module index |
| [../modules/contact-form/MODULE.md](../modules/contact-form/MODULE.md) | Contact form deploy runbook |

### Agents & security

| Document | What you'll learn |
|----------|-------------------|
| [agents.md](agents.md) | Agent directory layout and lifecycle |
| [sandbox.md](sandbox.md) | Cursor sandbox, scope fence, per-agent policy |
| [../SECURITY.md](../SECURITY.md) | Secrets policy and pre-push audit |

### Dashboard & API

| Document | What you'll learn |
|----------|-------------------|
| [app-context.md](app-context.md) | Dashboard UI, registry, WebSocket terminal |
| [api-reference.md](api-reference.md) | REST + WebSocket routes |
| [xterm.md](xterm.md) | In-browser tmux terminal |

### Operations

| Document | What you'll learn |
|----------|-------------------|
| [troubleshooting.md](troubleshooting.md) | Common failures and fixes |

---

## Scripts

| Script | Purpose |
|--------|---------|
| [../scripts/check-prerequisites.sh](../scripts/check-prerequisites.sh) | Verify Python, tmux, Cursor, deps before deploy |
| [../scripts/audit-secrets.sh](../scripts/audit-secrets.sh) | Scan for credential leaks before git push |
| [../scripts/reinject-rc.py](../scripts/reinject-rc.py) | Bulk-refresh agent RC copies from master |

---

## Reference material

| Path | Purpose |
|------|---------|
| [../MASTER-CONTEXT.md](../MASTER-CONTEXT.md) | System rules every agent reads |
| [../agents/_example/](../agents/_example/) | Example agent scaffold |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | How to contribute safely |

---

## GitHub tip

Pin **Documentation** in your repo's **About** section to `docs/README.md` so
this index appears on the repository home page sidebar.
