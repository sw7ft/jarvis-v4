# mailinbox.py — Mail-in-a-Box Email App

## Overview

`mailinbox.py` is a lightweight, stdlib-only Python email client for JARVIS v4 agents. It connects to a Mail-in-a-Box server via IMAP (read) and SMTP (send), giving each agent its own dedicated mailbox.

Each agent gets its own copy at `agents/<name>/apps/mailinbox.py` with credentials baked in at deploy time — no config files needed at runtime.

---

## Architecture

```
jarvisv4/apps/mailinbox.py          ← master copy (DEFAULT_* stubs empty)
       ↓ deploy.py --mailinbox-*
agents/<name>/apps/mailinbox.py     ← per-agent copy (credentials injected)
       ↓ agent runs
mail.example.com
  IMAP :993 SSL       ← read inbox, list folders, fetch email body
  SMTP :587 STARTTLS  ← send email
```

---

## Deploying to an Agent

When creating or redeploying an agent, pass the mailbox credentials:

```bash
python3 deploy.py <agent.name> \
  --mailinbox-host mail.example.com \
  --mailinbox-email agent@mail.example.com \
  --mailinbox-password '<your-password>'
```

This injects the credentials into the top of `agents/<name>/apps/mailinbox.py`:

```python
DEFAULT_HOST     = 'mail.example.com'
DEFAULT_EMAIL    = 'agent@mail.example.com'
DEFAULT_PASSWORD = '<your-password>'
DEFAULT_INBOX    = 'INBOX'
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 587
```

If `--mailinbox-*` flags are omitted, the script is still copied with empty stubs. Run the setup wizard later:

```bash
python3 agents/<name>/apps/mailinbox.py setup
```

---

## CLI Reference

All commands are run from the agent's working directory (`agents/<name>/`):

### `inbox` — List recent emails

```bash
python3 apps/mailinbox.py inbox
python3 apps/mailinbox.py inbox --count 20
python3 apps/mailinbox.py inbox --folder Sent
```

Output: table of UID, date, from, subject. Use the UID with `read`.

### `read` — Read a single email

```bash
python3 apps/mailinbox.py read <uid>
python3 apps/mailinbox.py read <uid> --folder Sent
```

Fetches the plain-text body. Long bodies are truncated at 5000 chars.

### `send` — Send an email

```bash
python3 apps/mailinbox.py send recipient@example.com "Subject here" "Body text here"
```

Sends via SMTP STARTTLS on port 587 from the agent's configured address.

### `folders` — List IMAP folders

```bash
python3 apps/mailinbox.py folders
```

### `test` — Verify IMAP + SMTP connectivity

```bash
python3 apps/mailinbox.py test
```

Checks IMAP login, selects INBOX, counts messages. Then checks SMTP auth. No email is sent. Returns exit code 0 on success.

### `setup` — Interactive config wizard

```bash
python3 apps/mailinbox.py setup
```

Saves credentials to `~/.config/mailinbox/config.json` (mode 600). Only needed for the master copy or manual use — per-agent copies use injected constants.

---

## Config Priority

1. **Injected constants** at the top of the per-agent script (set by `deploy.py`) — always used first
2. **`~/.config/mailinbox/config.json`** — fallback for master copy / manual use

---

## Dashboard Integration

The JARVIS v4 dashboard (`app.py`) shows a blue mail icon in each agent card's right dock **if `mailinbox.py` exists in the agent's `apps/` directory**.

Click the icon to see:
- Host, email address, inbox folder, IMAP/SMTP ports
- **Test Connection** button — runs `mailinbox.py test` live and shows output

---

## Mail-in-a-Box Setup (server side)

Each agent needs a dedicated mailbox on the Mail-in-a-Box server:

1. Log in to your Mail-in-a-Box admin (e.g. `https://mail.example.com/admin`)
2. Go to **Mail → Users → Add User**
3. Create `agent@yourdomain.example` with a strong password
4. Use these settings in deploy.py:
   - Host: `mail.example.com`
   - Email: `agent@yourdomain.example`
   - Password: as set above

---

## Dependencies

None. Uses Python stdlib only:

| Module | Purpose |
|--------|---------|
| `imaplib` | IMAP4 client (read mail) |
| `smtplib` | SMTP client (send mail) |
| `email` | Parse/build RFC 822 messages |
| `ssl` | TLS context for secure connections |
| `json` | Config file read/write |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `No mailinbox config found` | Constants empty, no config file | Re-deploy with `--mailinbox-*` flags or run `setup` |
| `IMAP FAIL — [SSL: CERTIFICATE_VERIFY_FAILED]` | Self-signed cert on dev server | Add cert to system trust store, or check hostname |
| `SMTP FAIL — 535 Authentication failed` | Wrong password | Verify credentials in Mail-in-a-Box admin |
| `IMAP FAIL — LOGIN failed` | Wrong email or password | Double-check `DEFAULT_EMAIL` and `DEFAULT_PASSWORD` at top of script |
| Emails show garbled characters | Charset decode issue | Usually harmless; body decoded with `errors="replace"` |
| `read <uid>` returns empty body | HTML-only email | Plain-text part not present; check sender's email client settings |
