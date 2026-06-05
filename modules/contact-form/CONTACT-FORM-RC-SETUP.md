# Form Submissions → Rocket.Chat Setup

All website forms (contact + newsletter) post to a Rocket.Chat channel via Incoming Webhook and are logged to a file for redundancy.

## Forms Covered

| Form | Fields |
|------|--------|
| Contact | Name, email, phone, community, subject, message |
| Newsletter (simple) | Email |
| Newsletter (full) | First name, last name, email, community |
| Chat widget | Email (required), message |

## 1. Create Incoming Webhook in Rocket.Chat

1. Log in to Rocket.Chat at **your server URL** (e.g. `https://chat.example.com`) as an admin
2. **Administration** → **Workspace** → **Integrations**
3. **New** → **Incoming Webhook**
4. Configure:
   - **Name:** `Website Contact Form`
   - **Post to Channel:** `#your_channel` (match the agent channel)
   - **Enable Script:** Off (leave default)
5. **Save** and copy the **Webhook URL** (e.g. `https://chat.example.com/hooks/xxxxx/yyyyy`)

Or use the webhook created automatically when you run `python3 deploy.py <agent.name>` — see `DEFAULT_WEBHOOK_URL` in the agent's `rocketchat.py`.

## 2. Configure the Server

On the client server:

```bash
ssh <target>
cd ~/public_html
cp contact-webhook-config.php.example contact-webhook-config.php
nano contact-webhook-config.php   # Paste webhook URL
```

Set `webhook_url` to the URL you copied from Rocket.Chat.

## 3. Redundancy: Log File

All submissions are logged to `~/logs/form-submissions.log` (outside web root). Format:

```
2026-03-19 20:30:00	contact	{"email":"...","firstName":"...","lastName":"...","phone":"...","community":"...","subject":"...","message":"..."}
2026-03-19 20:31:00	newsletter	{"email":"...","firstName":"...","lastName":"...","phone":null,"community":"...","subject":null,"message":null}
```

## 4. Files Deployed

| File | Purpose |
|------|---------|
| `contact-submit.php` | Handles contact + newsletter; validates, logs, forwards to RC |
| `contact-webhook-config.php` | Holds webhook URL (create from `.example`) |

## 5. Message Format in Rocket.Chat

**Contact form:**
```
**New contact form submission**

**Name:** John Smith
**Email:** john@example.com
**Phone:** (555) 555-0100
**Community:** Example City
**Subject:** Constituent Issue

**Message:**
Tell us how we can help…
```

**Newsletter signup:**
```
**New newsletter signup**

**Name:** Jane Doe
**Email:** jane@example.com
**Community:** Example City
**Source:** https://example.com/stay-informed.html
```

**Chat widget:**
```
**New message from chat widget**

**Email:** visitor@example.com

**Message:**
I have a question about services.

**Source:** https://example.com/about.html
```

## 6. Troubleshooting

- **"Form is not configured"** — Create `contact-webhook-config.php` and set `webhook_url`
- **"Could not send your message"** — Check webhook URL is correct; verify channel exists
- Check PHP error log on the server (path varies by host)
