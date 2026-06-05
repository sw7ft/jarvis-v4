# Security

## Reporting vulnerabilities

If you find a security issue in JARVIS v4, please **do not** open a public GitHub
issue with exploit details. Contact the repository maintainer privately.

---

## Secrets — never commit these

| Secret | Where it belongs |
|--------|------------------|
| Rocket.Chat admin/bot passwords | `~/.config/rocketchat/config.json` (chmod 600) |
| Mail IMAP/SMTP passwords | Injected into `agents/<name>/apps/mailinbox.py` (gitignored) |
| Incoming webhook URLs | Agent `rocketchat.py` or server `contact-webhook-config.php` |
| SSH private keys | `~/.ssh/` |
| API keys / tokens | App `DEFAULT_*` constants or env vars — not in git |

This repository's `.gitignore` excludes:

- `agents/*/` except `agents/_example/` scaffold
- `agents/_example/apps/` (injected copies)
- `**/browser-profile/`, logs, `data/`

---

## Before you push

```bash
# Quick leak scan (run from repo root)
./scripts/audit-secrets.sh
```

Do not commit if the script reports real credentials, webhook tokens, or
production hostnames you did not intend to share.

---

## Dashboard exposure

`app.py` binds to `0.0.0.0:5112` by default. Treat it as **localhost-only**
unless you add authentication and TLS in front. The API can read agent config
and trigger deploys.

---

## Agent sandbox

Agents run with Cursor CLI and can read files inside their agent directory.
Do not store other clients' credentials in an agent's `docs/` or `utilities/`.

See [docs/sandbox.md](docs/sandbox.md).
