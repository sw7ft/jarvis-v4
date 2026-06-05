# Module: contact-form

**Category:** website
**Ships to:** remote server web root (via scp)

## What It Does

Handles all website form submissions from a single PHP endpoint and forwards
them to the agent's RocketChat channel via an Incoming Webhook. Supports five
form types: contact, newsletter, chat widget, take-action, and survey. All
submissions are also written to a log file on the server for redundancy.
Includes PHP-only spam prevention (honeypot, time lock, rate limit, URL cap).

No curl required — uses PHP's `file_get_contents()` + `stream_context_create()`
which works on shared hosting environments.

## Files in This Module

| File | Ships to server | Purpose |
|------|----------------|---------|
| `contact-submit.php` | `<web-root>/contact-submit.php` | Main form handler — validates, logs, posts to RC webhook |
| `contact-webhook-config.php.example` | `<web-root>/contact-webhook-config.php.example` | Config template — copy to `contact-webhook-config.php` and set webhook URL |
| `CONTACT-FORM-RC-SETUP.md` | stays local (reference) | Step-by-step guide for creating the RC Incoming Webhook |

## Config Required

Before deploying, gather these values:

| Variable | Where to get it |
|----------|----------------|
| `SSH_HOST` | agent's Identity table → SSH host (e.g. `example.com`) |
| `WEB_ROOT` | server web root path (e.g. `/home/example.com/public_html`) |
| `SITE_URL` | live site URL (e.g. `https://example.com`) |
| `WEBHOOK_URL` | create in RC Admin → see `CONTACT-FORM-RC-SETUP.md` |

## Deploy Steps

### 1. Create the RocketChat Incoming Webhook

Read `CONTACT-FORM-RC-SETUP.md` in this directory for the full walkthrough.
Quick version:
- RC Admin → Workspace → Integrations → New → Incoming Webhook
- Post to channel: `#<agent-channel>`
- Save → copy the webhook URL

Or check if one already exists for this agent:

```bash
python3 apps/rocketchat.py webhooks
```

### 2. Ship PHP files to the server

From the `jarvisv4/` root directory:

```bash
scp modules/contact-form/contact-submit.php <SSH_HOST>:<WEB_ROOT>/
scp modules/contact-form/contact-webhook-config.php.example <SSH_HOST>:<WEB_ROOT>/
```

### 3. Create the webhook config on the server

```bash
ssh <SSH_HOST> "cd <WEB_ROOT> && cp contact-webhook-config.php.example contact-webhook-config.php"
ssh <SSH_HOST> "cd <WEB_ROOT> && sed -i 's|https://chat.example.com/hooks/YOUR_WEBHOOK_ID/YOUR_TOKEN|<WEBHOOK_URL>|' contact-webhook-config.php"
```

Or SSH in and edit manually:

```bash
ssh <SSH_HOST>
cd <WEB_ROOT>
cp contact-webhook-config.php.example contact-webhook-config.php
nano contact-webhook-config.php   # paste webhook URL
```

### 4. Ensure the log directory exists on the server

The handler logs to `~/logs/form-submissions.log` (one level above web root).

```bash
ssh <SSH_HOST> "mkdir -p ~/logs"
```

### 5. Verify the handler responds

```bash
curl -s -X POST <SITE_URL>/contact-submit.php \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'formType=contact&firstName=Test&lastName=User&email=test@example.com&message=hello+from+module+test'
```

Expected response: `{"ok":true}`

### 6. Confirm the RC message arrived

Check the agent's channel in RocketChat — you should see:

```
**New contact form submission**
**Name:** Test User
**Email:** test@example.com
**Subject:**
**Message:**
hello from module test
```

### 7. Wire up your HTML forms

Add to any contact form:
```html
<input type="hidden" name="formType" value="contact">
<input type="hidden" name="formLoadTime" id="formLoadTime" value="">
<input type="text" name="website" style="display:none" tabindex="-1" autocomplete="off">
```

Add to page JS (sets the time-lock field on load):
```js
document.getElementById('formLoadTime').value = Date.now() / 1000;
```

Submit via fetch to `/contact-submit.php`.

## Form Types Reference

| `formType` value | Required fields | Optional fields |
|-----------------|-----------------|-----------------|
| `contact` | firstName, lastName, email, message | phone, community, subject |
| _(none/newsletter)_ | email | firstName, lastName, community |
| `chat` | email, message | — |
| `takeaction` | firstName, email | lastName, community, interests |
| `survey` | email | firstName, lastName, community, issues, topPriority, rightTrack, comments |

## Spam Prevention (built-in, no config needed)

| Check | Details |
|-------|---------|
| Honeypot | Hidden `website` / `url` / `company_name` field — bots fill it, humans don't |
| Time lock | Form must be open ≥3 seconds (`formLoadTime` hidden field) |
| Rate limit | 5 submissions/hour per IP (temp file, no DB needed) |
| URL cap | ≤2 links allowed in message body |
| Length cap | Message max 2000 chars |

## Log Format

All submissions append to `~/logs/form-submissions.log` on the server:

```
2026-04-20 14:30:00	contact	{"email":"...","firstName":"...","lastName":"...","phone":"...","community":"...","subject":"...","message":"..."}
2026-04-20 14:31:00	newsletter	{"email":"...","firstName":"...","lastName":"...","phone":null,"community":"...","subject":null,"message":null}
```

Newsletter signups also append to `<web-root>/email.json` for mailing list use.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `{"ok":false,"error":"Form is not configured"}` | `contact-webhook-config.php` missing or empty webhook_url | Create from `.example`, set `webhook_url` |
| `{"ok":false,"error":"Could not send your message"}` | Webhook URL wrong, channel missing, or server can't reach RC | Verify URL; check channel exists in RC |
| `{"ok":false,"error":"Please fill in all required fields"}` | formType=contact but missing firstName/lastName/email | Check form fields have correct `name` attributes |
| `{"ok":false,"error":"Please enter your email address"}` | Detected as newsletter (no formType or message) | Add `<input type="hidden" name="formType" value="contact">` |
| Form submits but no RC message | Webhook URL correct but channel gone | Verify channel exists; recreate webhook if needed |
| Log file not writing | `~/logs/` dir missing or permission denied | `ssh <host> "mkdir -p ~/logs"` |
