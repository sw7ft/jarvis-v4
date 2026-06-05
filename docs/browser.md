# browser.py — Persistent Playwright Browser App

## Overview

`browser.py` gives each JARVIS v4 agent its own dedicated, long-lived Chrome
browser — a real logged-in profile that stays on disk between calls. The agent
drives it from the command line; the dashboard shows a popover for humans.

It's the visual sibling to `mailinbox.py`: same install/inject lifecycle, same
APPS_REGISTRY pattern, same popover-in-the-right-dock pattern. The difference is
that Chrome is a **persistent process** (one per agent), not a request/response
client.

---

## Architecture

```
jarvisv4/apps/browser.py             ← master copy (DEFAULT_* stubs)
       ↓ deploy.py
agents/<name>/apps/browser.py        ← per-agent copy, constants injected
       ↓ first nav command auto-launches
Chrome (system /Applications/Google Chrome.app)
  --user-data-dir agents/<name>/browser-profile     ← cookies, logins persist
  --remote-debugging-port <PORT>                    ← deterministic per agent

agents/<name>/browser-profile/
  ├── (Chrome's actual profile contents — gitignored)
  ├── .jarvis-meta.json    ← {pid, port, started_at, last_url, ...}
  ├── chrome.log           ← Chrome stderr/stdout for debugging
  └── screenshots/         ← saved screenshots
```

Each agent gets:

| Field | Value | Source |
|---|---|---|
| Profile name | the agent name | injected |
| Profile dir  | `agents/<name>/browser-profile/` | injected (absolute path) |
| CDP port     | deterministic 9300–9999 hash of name | injected |
| Chrome path  | system Google Chrome (auto-detected) | injected |

Strict 1:1:1: one agent → one profile dir → one CDP port → one Chrome PID. Never
share profile dirs; Chrome refuses to open the same one twice.

---

## Deploying

Browser is **opt-in** — install via dashboard **+ → Browser → Install**, or
`POST /api/apps/install` with `app_id: browser`. It is not copied during
`deploy.py` (unlike `rocketchat.py` and the mailinbox stub).

After install, constants are derived from the agent name (port, profile dir,
Chrome path). No CLI flags needed.

To check what got injected:

```bash
head -25 agents/<name>/apps/browser.py | grep DEFAULT_
```

---

## CLI Reference

All commands run from the agent's working directory (`agents/<name>/`). Append
`--json` to any command for machine-parseable output.

### Lifecycle

| Command | What it does |
|---|---|
| `launch [--headless]` | Start Chrome for this profile. First launch should be **headed** so you can log in to sites by hand. Subsequent runs persist those logins. |
| `stop` | SIGTERM (then SIGKILL) the Chrome process. Clears the meta file. |
| `status` | Running? pid? port? last URL? headless? |
| `test` | Auto-launches if needed, attaches via CDP, fetches a page title, disconnects. Verifies the whole pipe end-to-end. |

### Navigation

| Command | What it does |
|---|---|
| `goto <url>` | Navigate the primary tab. Returns final URL + title + HTTP status. |
| `back` / `forward` / `reload` | Self-explanatory. |
| `wait <selector> [--timeout 10]` | Block until a CSS selector appears. |
| `snapshot [--format text\|html] [--full]` | Dump the page. `text` (default) = `body.innerText`; `html` = full DOM. Truncated to 8k chars unless `--full`. |
| `extract <selector>` | `innerText` of all matching elements (up to 50). |

### Interaction

| Command | What it does |
|---|---|
| `click <selector>` | Click the first matching element. |
| `type <selector> <text>` | Type into a field (appends, doesn't clear). |
| `fill <selector> <text>` | Clear then set a field's value. |
| `screenshot [--path FILE] [--full-page]` | Save PNG. Default path: `browser-profile/screenshots/screenshot-<ts>.png`. |
| `eval '<js>'` | Run JS in the page. Bare expressions are auto-wrapped; arrow funcs and `function() {}` pass through. |

### Tabs

| Command | What it does |
|---|---|
| `tabs` (or `tabs list`) | List open tabs with index + title + URL. |
| `tabs new [--url URL]` | Open a new tab. |
| `tabs switch <n>` | Bring tab N to front. |
| `tabs close <n>` | Close tab N. |

### Site-knowledge files (`context`)

Per-agent, per-site markdown notes live in `agents/<name>/apps/browser-context/<domain>.md`,
sitting next to the agent's `browser.py`. Use them to give the agent persistent
knowledge about a site's structure (nav, forms, login flow, gotchas) so it
doesn't have to re-learn the layout every session.

| Command | What it does |
|---|---|
| `context list` | List all stored site files (size, mtime). |
| `context show <domain>` | Print one file. |
| `context write <domain> [--text "..." \| stdin]` | Save (overwrites). |
| `context append <domain> [--text "..." \| stdin]` | Append a note. |
| `context auto [<domain>] [--url URL]` | Visit URL (or use current page), scrape title / description / canonical / H1-H3 / nav links / forms, write a markdown file. |
| `context rm <domain>` | Delete. |
| `context path [<domain>]` | Print the dir or a specific file's path. |

**Domain normalization**: any URL or `.md` filename gets normalized into a clean
lowercase domain. `https://www.impactauto.ca/about`, `IMPACTAUTO.CA.md`, and
`impactauto.ca` all resolve to `impactauto.ca`.

**Notes-preservation contract**: `context auto` regenerates everything above
the `## Notes` marker, but never touches what comes after it. So you can run
`auto` whenever a site changes structurally and your hand-written annotations
survive intact. Example layout:

```markdown
# impactauto.ca
**Title**: ...
**Description**: ...
## H1 / H2 / H3 ...
## Navigation Links ...
## Forms ...

## Notes
<!-- Below this line is hand-written, auto never overwrites. -->
- Seller login is at https://seller.impactauto.ca/
- Captcha appears after 3 failed logins
```

Typical agent workflow:

```bash
python3 apps/browser.py goto "https://impactauto.ca"
python3 apps/browser.py context auto                     # bootstrap site file
python3 apps/browser.py context show impactauto.ca       # read what's known
python3 apps/browser.py context append impactauto.ca \
  --text "- The 'View Inventory' button is .btn-inventory"
```

---

## Auto-launch behavior

Like `mailinbox.py`'s connection model: the file is installed at deploy time
with no side effects. Chrome only starts when someone asks for it.

- The first time the agent (or dashboard) runs **any** navigation command
  (`goto`, `click`, `snapshot`, ...), Chrome auto-launches **headed** so the
  user can complete any first-time logins.
- Subsequent commands reattach over CDP to the already-running Chrome —
  fast (~200ms).
- Explicit `launch` / `stop` are available for the dashboard popover and for
  manual control. Pass `--headless` to `launch` for production-style runs.

When Chrome dies (Mac sleep, crash, user closes the window), the next nav
command transparently relaunches it. Cookies / logins survive because they
live on disk in the profile dir.

---

## Dashboard Integration

The dashboard (`app.py`) shows a purple **Browser** icon in each agent card's
right dock when the agent has `browser.py` installed.

Click it to get a popover with:

- **Status**: green dot when Chrome is running with a live pid + open CDP port
- **PID / Port / Last URL / Started / Profile path**
- **URL bar + Go** → drives `browser.py goto` directly from the dashboard
- **Launch / Stop / Snapshot / Test** buttons
- **Snapshot** renders the current page as a PNG right in the popover, useful
  to confirm what the agent's actually looking at

Routes (mirror of `/api/mailinbox/*`):

| Method | Route | Action |
|--------|-------|--------|
| GET    | `/api/browser/config/<name>`     | Injected constants + live state |
| POST   | `/api/browser/launch`            | Start Chrome |
| POST   | `/api/browser/stop`              | Kill Chrome |
| POST   | `/api/browser/test`              | CDP attach test |
| POST   | `/api/browser/goto`              | `{name, url}` |
| GET    | `/api/browser/screenshot/<name>` | Fresh PNG of the current page |

---

## Setup (one-time per machine)

```bash
pip3 install playwright
# Chromium NOT needed — we drive the system Google Chrome over CDP
```

Make sure Google Chrome is installed (this is the default on most dev Macs).
If Chrome lives somewhere unusual, edit `DEFAULT_CHROME_PATH` in the agent's
`browser.py` after deploy.

---

## Port allocation

Each agent gets a deterministic CDP port in **9300–9999**, derived from the
agent name via SHA-1. Same agent name → same port across every re-deploy and
machine reboot. Collisions are theoretically possible but vanishingly rare for
typical fleets (<100 agents).

Look up an agent's port:

```bash
grep DEFAULT_CDP_PORT agents/<name>/apps/browser.py
```

Or `python3 apps/browser.py status` from inside the agent dir.

---

## Storage and cleanup

Profiles can grow to **100MB+** (cookies, cache, IndexedDB, extensions).
They live in `agents/<name>/browser-profile/` and are **gitignored** — never
committed.

To reset a profile (logs out of everything, clears all state):

```bash
python3 agents/<name>/apps/browser.py stop
rm -rf agents/<name>/browser-profile
# next nav command will create a fresh empty profile
```

---

## Anti-detection notes

The current implementation uses vanilla Playwright + system Chrome. This is
indistinguishable enough from a real user for most sites (Gmail, Twitter,
LinkedIn casual browsing, most SaaS dashboards). For sites with aggressive
bot detection (Cloudflare turnstile, Akamai), consider:

- `playwright-stealth` (drop-in patches for `navigator.webdriver` etc.)
- `patchright` (a hardened fork that goes further)
- Slowing down keystrokes / clicks (the human-cadence trick)
- Always doing the first login **headed** to clear "new device" challenges

These are deliberate Phase 2 additions — not needed for v1.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Playwright is not installed` | `pip3 install playwright` not run | `pip3 install playwright` |
| `Google Chrome not found` | Chrome not in standard path | Install Chrome, or edit `DEFAULT_CHROME_PATH` |
| `Chrome did not open CDP port within 30s` | Another Chrome already using the same profile, or port collision | `ps aux \| grep Chrome` — kill stragglers; check `lsof -i :<port>` |
| `connect_over_cdp failed` | Chrome died after launch | Check `agents/<name>/browser-profile/chrome.log` |
| Logged out after a few days | Site invalidated session | Re-login headed; some sites (Google, Microsoft) re-auth every 30d regardless |
| Works headed, fails headless | First-device challenge / bot detection | Launch headed once to clear; consider stealth (Phase 2) |
| Multiple Chrome windows opening | `--user-data-dir` corrupt | `rm -rf agents/<name>/browser-profile` and restart |
| Popover "Snapshot" button shows old image | Browser cached the PNG | Already cache-busted with `?t=<ms>` — try again, or hard refresh dashboard |

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `playwright` (Python) | CDP client, page interaction API |
| Google Chrome (system) | The actual browser |

No `playwright install chromium` needed — Chromium is bundled with the pip
package but we deliberately ignore it in favor of system Chrome (smaller
fingerprint, real user-grade extensions, faster startup on first launch).
