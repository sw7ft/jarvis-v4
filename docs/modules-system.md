# Modules — complete guide

**Modules** are optional, self-contained packages in `modules/<name>/` that extend
what agents can deploy to **remote servers** or local paths. Unlike **apps**
(which run on the MacBook inside `agents/<name>/apps/`), modules are **shipped
outward** — PHP to a web root, config snippets to a server, etc.

This document explains the module philosophy, anatomy, how agents deploy them,
the contact-form module in depth, and how to author a new module.

**Related:** [apps-system.md](apps-system.md) (apps vs modules) ·
[rocketchat-integration.md](rocketchat-integration.md) (webhooks)

---

## Apps vs modules

| | Apps | Modules |
|---|------|---------|
| **Location** | `jarvisv4/apps/` → copied to `agents/<name>/apps/` | `modules/<name>/` stays in repo |
| **Runs where** | Agent host (MacBook) | Target server / remote path |
| **Install mechanism** | `deploy.py` + dashboard registry | Agent reads `MODULE.md`, runs scp/ssh |
| **Registry** | `APPS_REGISTRY` in `app.py` | `modules/README.md` index table |
| **Credentials** | Injected into Python constants | Config files on remote server |
| **Example** | `mailinbox.py`, `browser.py` | `contact-form` PHP handler |

```
MacBook (JARVIS)                         Remote server (client site)
├── agents/client.com/                   ├── public_html/
│   └── apps/rocketchat.py  ──webhook──►│   ├── contact-submit.php  ← from module
│       (monitor sees RC message)        │   └── contact-webhook-config.php
└── modules/contact-form/                └── ~/logs/form-submissions.log
    ├── MODULE.md  ← deploy instructions
    └── contact-submit.php
```

When a website visitor submits a form, PHP POSTs to Rocket.Chat via **incoming
webhook**. The agent's RC monitor picks up the message like any other human
post — no special code path required.

---

## Design principles

1. **Self-contained** — everything needed to deploy lives in the module folder.
2. **MODULE.md is truth** — agents and operators follow it verbatim; no need to
   read JARVIS core code.
3. **No deploy.py changes** — modules do not require framework patches.
4. **Agent-owned deploy** — the Cursor agent SSHs, scps, verifies; documents
   what it did in `context.md`.
5. **RC as bus** — modules that notify operators almost always use the agent's
   existing incoming webhook URL (created at deploy time).

---

## Module anatomy

Every module directory follows this layout:

```
modules/<module-name>/
├── MODULE.md              ← READ FIRST — deploy steps, verify, rollback
├── <files to ship>        ← PHP, shell, config templates
├── *.example              ← config templates copied on server
└── <optional docs>        ← setup guides that stay local
```

### MODULE.md required sections

| Section | Content |
|---------|---------|
| Title + category | e.g. `# Module: contact-form` / website |
| What it does | One paragraph |
| Files table | Source file → destination path on server |
| Config required | SSH host, web root, webhook URL, etc. |
| Deploy steps | Numbered, copy-paste commands |
| Verify | curl test, expected RC message |
| Troubleshooting | Symptom → cause → fix table |

See `modules/contact-form/MODULE.md` as the reference implementation.

### Index registration

Add a row to `modules/README.md`:

```markdown
| `my-module` | category | One-line description |
```

The dashboard **Modules** file browser reads this tree — grouped folders with
collapsible sections and auto-loaded README.

---

## How agents deploy modules

There is no `python3 deploy.py --module contact-form`. The workflow is intentional:

### Operator or human request

> "Deploy the contact-form module to example.com's server."

### Agent procedure

1. **Read** `modules/contact-form/MODULE.md` from repo root (or via dashboard).
2. **Gather config** from `context.md` identity table:
   - `SSH_HOST` — client's server
   - `WEB_ROOT` — e.g. `/home/example.com/public_html`
   - `WEBHOOK_URL` — from agent's injected `rocketchat.py` or RC admin
3. **Execute deploy steps** — typically `scp` + `ssh` commands from MODULE.md.
4. **Verify** — curl POST test, confirm message in `#example.com`.
5. **Document** — append to agent's `context.md` under a "Deployed modules"
   section (paths, webhook, log locations).

### Why not automate in deploy.py?

Modules target **heterogeneous remote infrastructure** (shared hosting, nginx
vs apache, custom paths). A human-readable runbook scales better than hard-coding
every hosting variant in Python. Apps target **homogeneous local execution**
(one MacBook, one Python env) — that's why apps get injection machinery.

---

## Discovering modules

```bash
# Index
cat modules/README.md

# Full deploy guide for one module
cat modules/contact-form/MODULE.md

# Dashboard
python3 app.py → nav "Modules" → browse tree
```

Agents should be told in `MASTER-CONTEXT.md` / `context.md`:

> When asked to deploy a module, read `modules/<name>/MODULE.md` first.

---

## Module: contact-form (deep dive)

**Category:** website  
**Ships to:** client web root via `scp`  
**Depends on:** Rocket.Chat incoming webhook for the agent's channel

### Purpose

Single PHP endpoint (`contact-submit.php`) handles multiple form types from a
client website and forwards formatted submissions to the agent's Rocket.Chat
channel. Also logs every submission to disk for redundancy and newsletter emails
to a local JSON file.

### End-to-end flow

```
Visitor fills form on https://client.com/contact.html
    ↓ POST /contact-submit.php
contact-submit.php (on client server)
    ├── validate + spam checks
    ├── append ~/logs/form-submissions.log
    ├── (newsletter) append public_html/email.json
    └── POST webhook URL → Rocket.Chat
            ↓
#client.com channel — formatted markdown message
            ↓ poll (10s)
agents/client.com — rocketchat.py monitor
            ↓ dispatch
Cursor agent in tmux pane 1 — sees new lead, responds
```

### Files

| File | Stays local | Ships to server | Role |
|------|-------------|-----------------|------|
| `MODULE.md` | ✓ | | Deploy runbook |
| `CONTACT-FORM-RC-SETUP.md` | ✓ | | RC webhook creation guide |
| `contact-submit.php` | | `<WEB_ROOT>/` | Main handler |
| `contact-webhook-config.php.example` | | `<WEB_ROOT>/` | Template → copy to `.php` |

### Webhook URL — two ways to get it

**A. Created automatically at agent deploy**

`deploy.py` registers an incoming webhook when creating the agent. Read it from
the injected agent copy:

```bash
grep DEFAULT_WEBHOOK_URL agents/client.com/apps/rocketchat.py
```

Or list all webhooks (admin):

```bash
python3 apps/master-rocketchat.py webhooks
```

**B. Manual creation**

Follow `modules/contact-form/CONTACT-FORM-RC-SETUP.md`:

1. RC Admin → Workspace → Integrations → New → Incoming Webhook
2. Post to channel: `#client.com`
3. Save → copy URL

The webhook URL is **per-channel**. Each agent/channel pair typically has one
webhook used by both contact forms and other integrations.

### Deploy commands (from repo root)

Replace placeholders from the agent identity table:

```bash
SSH_HOST=client.com
WEB_ROOT=/home/client.com/public_html
WEBHOOK_URL='https://chat.example.com/hooks/xxxxx/yyyyy'
SITE_URL=https://client.com

scp modules/contact-form/contact-submit.php \
    ${SSH_HOST}:${WEB_ROOT}/

scp modules/contact-form/contact-webhook-config.php.example \
    ${SSH_HOST}:${WEB_ROOT}/

ssh ${SSH_HOST} "cd ${WEB_ROOT} && \
  cp contact-webhook-config.php.example contact-webhook-config.php && \
  sed -i.bak 's|YOUR_WEBHOOK_ID/YOUR_TOKEN|${WEBHOOK_URL#*hooks/}|' contact-webhook-config.php"

ssh ${SSH_HOST} "mkdir -p ~/logs"
```

On macOS sed for remote server, prefer manual `nano` edit if `sed -i` differs.

### Verify

```bash
curl -s -X POST ${SITE_URL}/contact-submit.php \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'formType=contact&firstName=Test&lastName=User&email=test@example.com&message=hello'

# Expected: {"ok":true}
```

Check `#client.com` in Rocket.Chat for the formatted submission block.

### Supported form types

| `formType` | Required fields | RC message title |
|------------|-----------------|------------------|
| `contact` | firstName, lastName, email, message | New contact form submission |
| _(empty)_ / newsletter | email | New newsletter signup |
| `chat` | email, message | New message from chat widget |
| `takeaction` | firstName, email | New take action signup |
| `survey` | email | New survey response |

### HTML integration (minimum)

Contact form hidden fields:

```html
<input type="hidden" name="formType" value="contact">
<input type="hidden" name="formLoadTime" id="formLoadTime" value="">
<!-- honeypot — must stay hidden -->
<input type="text" name="website" style="display:none" tabindex="-1" autocomplete="off">
```

Page load JS (time-lock spam check):

```js
document.getElementById('formLoadTime').value = Date.now() / 1000;
```

Submit via `fetch('/contact-submit.php', { method: 'POST', body: new FormData(form) })`.

### Spam prevention (built-in)

| Check | Behavior |
|-------|----------|
| Honeypot | Hidden `website` / `url` / `company_name` — bots fill, request silently succeeds |
| Time lock | Form open ≥ 3 seconds (`formLoadTime`) |
| Rate limit | 5 submissions/hour/IP (temp file) |
| URL cap | ≤ 2 links in message body |
| Length cap | Message max 2000 chars |

No external services — works on cheap shared hosting.

### Logging

**Server log** — `~/logs/form-submissions.log` (tab-separated):

```
2026-05-29 14:30:00	contact	{"email":"...","firstName":"...",...}
```

**Newsletter list** — `<WEB_ROOT>/email.json` (append-only JSON array)

Agents can SSH and `tail` the log when debugging missed RC messages.

### RC message format (contact)

```
**New contact form submission**

**Name:** Test User
**Email:** test@example.com
**Phone:**
**Community:**
**Subject:**

**Message:**
hello
```

The agent treats this like operator mail — triage, reply in channel, maybe SSH
to the server to check logs.

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Form is not configured` | Missing `contact-webhook-config.php` | Copy from `.example`, set URL |
| `Could not send your message` | Bad webhook or RC down | Verify URL; test with curl to RC |
| Form OK but no RC message | Webhook points to wrong/deleted channel | Recreate webhook; update config |
| Log not writing | `~/logs/` missing | `mkdir -p ~/logs` |
| Newsletter mis-detected | Missing `formType=contact` on contact form | Add hidden field |

Full table in `modules/contact-form/MODULE.md`.

---

## Modules and Rocket.Chat webhooks

Modules that post **into** RC use **incoming webhooks** (server → RC).

Apps that read **from** RC use the **REST API** with bot credentials (RC → agent).

```
                    INBOUND to agent                    OUTBOUND from site
                    ─────────────────                   ──────────────────
Website form  ──webhook POST──►  #channel  ──poll──►  monitor  ──►  agent

Agent reply   ◄── send API ────  #channel  ◄── dispatch ──  agent
```

Same channel, two directions, two mechanisms. The webhook URL is created once
per agent at deploy and stored in `DEFAULT_WEBHOOK_URL`. Modules reuse it;
they do not need separate RC app code on the server — only PHP `file_get_contents`
to POST JSON/text to the hook URL.

Security:

- Webhook URL is a secret — keep in `contact-webhook-config.php` outside git.
- Prefer webhooks scoped to one channel.
- Rotate webhook if URL leaks (RC admin → regenerate → update server config).

---

## Adding a new module (checklist)

### 1. Create directory

```bash
mkdir -p modules/my-module
```

### 2. Write MODULE.md

Use `contact-form/MODULE.md` as template. Include:

- Exact `scp` / `ssh` commands with `<PLACEHOLDER>` tokens
- Verify step with expected output
- Rollback (what to delete on server)

### 3. Ship files

Keep modules minimal — only what must land on the target. Large assets belong
in the client's repo, not JARVIS.

### 4. Register

Add row to `modules/README.md`.

### 5. Optional: agent context snippet

Document in `docs/modules-system.md` or a short `docs/my-module.md` if the
module needs more than MODULE.md (architecture diagrams, etc.).

### 6. Test on a real agent

Deploy to a staging server, verify RC message, have agent confirm in channel.

---

## Module ideas (not shipped yet)

Examples that fit the pattern:

| Module | Ships | RC integration |
|--------|-------|----------------|
| `status-page` | nginx config snippet | Webhook on downtime |
| `backup-notify` | cron shell script | POST backup result to channel |
| `wordpress-mu-plugin` | PHP plugin drop-in | Form → webhook |
| `nginx-log-tail` | systemd unit | Agent routine reads log, posts anomalies |

Each gets its own folder + MODULE.md — no core changes.

---

## Dashboard: Modules browser

In `app.py`, the file browser **Modules** section:

- Lists `modules/*/` as collapsible groups
- Auto-opens `README.md` or `MODULE.md` when you click a folder
- Read-only reference — deploy still happens via agent/SSH

Useful when an operator asks "what modules exist?" without opening the repo in an editor.

---

## Agent documentation after deploy

After deploying a module, the agent should append to `context.md`:

```markdown
## Deployed modules

### contact-form (2026-05-29)

- Handler: `https://client.com/contact-submit.php`
- Config: `/home/client.com/public_html/contact-webhook-config.php`
- Log: `~/logs/form-submissions.log` on server
- Webhook: same as DEFAULT_WEBHOOK_URL (channel #client.com)
- Verified: curl test returned {"ok":true}, RC message received
```

This survives redeploys (`context.md` is not overwritten) and gives future
sessions full context.

---

## Quick reference

```bash
# List modules
cat modules/README.md

# Contact form deploy guide
cat modules/contact-form/MODULE.md

# Agent webhook URL
grep DEFAULT_WEBHOOK_URL agents/example.com/apps/rocketchat.py

# Test live handler
curl -s -X POST https://example.com/contact-submit.php \
  -d 'formType=contact&firstName=A&lastName=B&email=a@b.com&message=hi'
```
