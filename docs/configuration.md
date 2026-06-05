# Configuration

Central reference for paths, credentials, environment variables, and tunables.

---

## Paths (defaults)

| Path | Purpose |
|------|---------|
| Repo root | Directory containing `deploy.py` |
| `agents/` | Live agent directories |
| `archive/` | Archived agents |
| `apps/` | Master app templates |
| `templates/` | Scaffold templates |
| `modules/` | Deployable modules |
| `docs/` | Documentation |

Dashboard: `python3 app.py` → **http://localhost:5112**

---

## Rocket.Chat credentials

**File:** `~/.config/rocketchat/config.json` (mode 600)

**Create:** `python3 apps/master-rocketchat.py setup`

Used by: `deploy.py` (channel/webhook), master admin CLI, dashboard RC feed.

Never commit this file.

---

## Mail credentials

**Per-agent:** injected into `agents/<name>/apps/mailinbox.py` header

**Optional fallback:** `~/.config/mailinbox/config.json` (master/manual use)

**Deploy flags:**

```bash
--mailinbox-host HOST
--mailinbox-email EMAIL
--mailinbox-password PASS
```

---

## Browser app

**Per-agent:** injected on install (dashboard or manual)

Derived automatically:

- `DEFAULT_PROFILE_DIR` → `agents/<name>/browser-profile/`
- `DEFAULT_CDP_PORT` → hash of name, range 9300–9999
- `DEFAULT_CHROME_PATH` → auto-detected system Chrome

**Dependency:** `pip install playwright`

---

## Cursor agent model

**Default:** `composer-2.5` (`deploy.py` and `app.py`)

**Per-agent override:** `agents/<name>/.cursor-model` (one line, model slug)

List slugs: `cursor agent --list-models`

Apply override: redeploy or dashboard Refresh (recreates tmux pane 1).

---

## deploy.py flags

| Flag | Default | Description |
|------|---------|-------------|
| `--interval N` | 10 | RC poll interval (seconds) |
| `--system-prompt S` | DEFAULT_PERSONA | Monitor persona string |
| `--no-webhook` | off | Skip webhook registration |
| `--no-channel` | off | Skip channel create |
| `--no-attach` | off | Don't attach tmux at end |
| `--no-launch` | off | Scaffold only, no tmux |
| `--dry-run` | off | Print actions only |
| `--mailinbox-*` | empty | Mail credentials |

---

## SSH

Agents expect `ssh <agent.name>` to work when remote ops are needed.

Configure in `~/.ssh/config` — not stored in the repo.

`deploy.py` warns if host alias is missing but still scaffolds the agent.

---

## Environment variables

| Variable | Used by | Purpose |
|----------|---------|---------|
| `JARVIS_V2_ROOT` | `migrate_v2.py`, dashboard | Path to legacy v2 install for migration UI |
| `JARVIS_RC_URL` | `migrate_v2.py`, `app.py` | Rocket.Chat base URL (migration + dashboard deep-links) |

---

## Dashboard data directory

Some installs use `data/` for planner/hibernation state (gitignored). Not
required for minimal open-source setup.

---

## Gitignore essentials

See `.gitignore`. Before publishing, run `./scripts/audit-secrets.sh`.

- `agents/*/` except `agents/_example/`
- `agents/_example/apps/` (injected)
- `**/browser-profile/`
- `data/`, `logs/`, `*.log`
- `.env`, `contact-webhook-config.php` (real webhook config on servers)
