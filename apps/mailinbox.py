#!/usr/bin/env python3
"""
mailinbox.py — JARVIS v4 Mail-in-a-Box email app.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 SETUP & CONFIG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 This is the MASTER copy at jarvisv4/apps/mailinbox.py.
 deploy.py copies it to agents/<name>/apps/mailinbox.py and
 injects per-agent credentials into the DEFAULT_* constants below.

 Each agent gets its own dedicated email account on the
 Mail-in-a-Box server (e.g. agent@mail.example.com).

 To deploy with credentials:
   python3 deploy.py <agent.name> \\
     --mailinbox-host mail.example.com \\
     --mailinbox-email agent@mail.example.com \\
     --mailinbox-password <password>

 Protocols:
   IMAP  — port 993, SSL   (read inbox)
   SMTP  — port 587, STARTTLS (send email)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 USAGE (from agent working directory)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   python3 apps/mailinbox.py inbox [--count N] [--folder F]
   python3 apps/mailinbox.py read <uid>
   python3 apps/mailinbox.py send <to> <subject> <body>
   python3 apps/mailinbox.py folders
   python3 apps/mailinbox.py test
   python3 apps/mailinbox.py setup        # interactive wizard (sets DEFAULT_*)

 Stdlib only — no pip dependencies required.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import argparse
import email
import email.header
import email.mime.text
import email.mime.multipart
import imaplib
import json
import smtplib
import ssl
import sys
import textwrap
from datetime import datetime
from pathlib import Path

# ─── Injected by deploy.py for per-agent copies ──────────────────────────────
# Empty here in the master; deploy.py overwrites these when it copies this file
# to agents/<name>/apps/mailinbox.py.
DEFAULT_HOST     = ""          # Mail-in-a-Box hostname e.g. "mail.example.com"
DEFAULT_EMAIL    = ""          # Agent's mailbox e.g. "agent@mail.example.com"
DEFAULT_PASSWORD = ""          # IMAP/SMTP password
DEFAULT_INBOX    = "INBOX"     # Default folder to read from
DEFAULT_IMAP_PORT = 993        # IMAP SSL port
DEFAULT_SMTP_PORT = 587        # SMTP STARTTLS port

# ─────────────────────────────────────────────────────────────────────────────

CONFIG_DIR  = Path.home() / ".config" / "mailinbox"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load_config() -> dict:
    """Load from injected constants first, fall back to ~/.config/mailinbox/config.json."""
    if DEFAULT_HOST and DEFAULT_EMAIL and DEFAULT_PASSWORD:
        return {
            "host":      DEFAULT_HOST,
            "email":     DEFAULT_EMAIL,
            "password":  DEFAULT_PASSWORD,
            "inbox":     DEFAULT_INBOX or "INBOX",
            "imap_port": DEFAULT_IMAP_PORT,
            "smtp_port": DEFAULT_SMTP_PORT,
        }
    try:
        return json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.is_file() else {}
    except Exception:
        return {}


def _save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)


def _cfg_or_exit() -> dict:
    cfg = _load_config()
    if not cfg.get("host") or not cfg.get("email") or not cfg.get("password"):
        sys.exit(
            "No mailinbox config found.\n"
            "Run: python3 apps/mailinbox.py setup\n"
            "Or deploy with: python3 deploy.py <name> --mailinbox-host ... --mailinbox-email ... --mailinbox-password ..."
        )
    return cfg


# ─── IMAP helpers ────────────────────────────────────────────────────────────

def _imap_connect(cfg: dict) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    M = imaplib.IMAP4_SSL(cfg["host"], cfg.get("imap_port", 993), ssl_context=ctx)
    M.login(cfg["email"], cfg["password"])
    return M


def _decode_header(raw: str) -> str:
    parts = email.header.decode_header(raw or "")
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def _fetch_envelope(M: imaplib.IMAP4_SSL, uid: bytes) -> dict:
    _, data = M.uid("fetch", uid, "(RFC822.HEADER)")
    if not data or not data[0]:
        return {}
    msg = email.message_from_bytes(data[0][1])
    return {
        "uid":     uid.decode(),
        "from":    _decode_header(msg.get("From", "")),
        "to":      _decode_header(msg.get("To", "")),
        "subject": _decode_header(msg.get("Subject", "(no subject)")),
        "date":    msg.get("Date", ""),
    }


def _fetch_body(M: imaplib.IMAP4_SSL, uid: str) -> str:
    _, data = M.uid("fetch", uid.encode(), "(RFC822)")
    if not data or not data[0]:
        return "(no data)"
    msg = email.message_from_bytes(data[0][1])
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                body_parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(body_parts) or "(empty body)"


# ─── SMTP helpers ─────────────────────────────────────────────────────────────

def _smtp_send(cfg: dict, to: str, subject: str, body: str):
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["From"]    = cfg["email"]
    msg["To"]      = to
    msg["Subject"] = subject
    msg["Date"]    = email.utils.formatdate(localtime=True)
    msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP(cfg["host"], cfg.get("smtp_port", 587)) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        s.login(cfg["email"], cfg["password"])
        s.sendmail(cfg["email"], [to], msg.as_string())


# ─── CLI commands ─────────────────────────────────────────────────────────────

def cmd_inbox(args):
    cfg = _cfg_or_exit()
    folder = args.folder or cfg.get("inbox", "INBOX")
    count  = max(1, args.count)

    M = _imap_connect(cfg)
    M.select(folder, True)
    _, data = M.uid("search", None, "ALL")
    uids = data[0].split() if data[0] else []

    if not uids:
        print("(inbox empty)")
        M.logout()
        return

    recent = uids[-count:][::-1]   # newest first
    print(f"\n{'UID':<8} {'DATE':<28} {'FROM':<32} SUBJECT")
    print("─" * 90)
    for uid in recent:
        env = _fetch_envelope(M, uid)
        from_short = env.get("from", "")[:30]
        subj_short = env.get("subject", "")[:45]
        date_short = env.get("date", "")[:26]
        print(f"{env.get('uid',''):<8} {date_short:<28} {from_short:<32} {subj_short}")
    print()
    M.logout()


def cmd_read(args):
    cfg = _cfg_or_exit()
    folder = args.folder or cfg.get("inbox", "INBOX")

    M = _imap_connect(cfg)
    M.select(folder, True)
    env  = _fetch_envelope(M, args.uid.encode())
    body = _fetch_body(M, args.uid)
    M.logout()

    print(f"\nFrom:    {env.get('from','')}")
    print(f"To:      {env.get('to','')}")
    print(f"Date:    {env.get('date','')}")
    print(f"Subject: {env.get('subject','')}")
    print("─" * 70)
    print(textwrap.fill(body, width=80) if len(body) < 5000 else body[:5000] + "\n… (truncated)")
    print()


def cmd_send(args):
    cfg = _cfg_or_exit()
    _smtp_send(cfg, args.to, args.subject, args.body)
    print(f"✓ Sent to {args.to}: {args.subject}")


def cmd_folders(args):
    cfg = _cfg_or_exit()
    M = _imap_connect(cfg)
    _, folder_list = M.list()
    M.logout()
    print("\nAvailable folders:")
    for f in folder_list:
        if f:
            print(f"  {f.decode()}")
    print()


def cmd_test(args):
    cfg = _cfg_or_exit()
    print(f"\nTesting mailinbox for {cfg['email']} @ {cfg['host']} …\n")

    # IMAP test
    try:
        M = _imap_connect(cfg)
        folder = cfg.get("inbox", "INBOX")
        M.select(folder, True)
        _, data = M.uid("search", None, "ALL")
        count = len(data[0].split()) if data[0] else 0
        M.logout()
        print(f"  ✓ IMAP OK  — {folder} has {count} messages")
    except Exception as e:
        print(f"  ✗ IMAP FAIL — {e}")
        sys.exit(1)

    # SMTP test (no actual send)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], cfg.get("smtp_port", 587)) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(cfg["email"], cfg["password"])
        print(f"  ✓ SMTP OK  — authenticated on port {cfg.get('smtp_port', 587)}")
    except Exception as e:
        print(f"  ✗ SMTP FAIL — {e}")
        sys.exit(1)

    print(f"\n  All checks passed for {cfg['email']}\n")


def cmd_setup(args):
    print("\n── mailinbox setup ──────────────────────────────────────")
    print("This wizard saves credentials to ~/.config/mailinbox/config.json")
    print("For per-agent deploy, use deploy.py --mailinbox-* flags instead.\n")

    def ask(prompt, default=""):
        val = input(f"  {prompt}{f' [{default}]' if default else ''}: ").strip()
        return val or default

    cfg = {
        "host":      ask("Mail-in-a-Box hostname (e.g. mail.example.com)"),
        "email":     ask("Email address"),
        "password":  ask("Password"),
        "inbox":     ask("Default inbox folder", "INBOX"),
        "imap_port": int(ask("IMAP port", "993")),
        "smtp_port": int(ask("SMTP port", "587")),
    }

    if not cfg["host"] or not cfg["email"] or not cfg["password"]:
        sys.exit("Host, email and password are required.")

    _save_config(cfg)
    print(f"\n  ✓ Config saved to {CONFIG_FILE} (mode 600)")
    print("\nRun `python3 apps/mailinbox.py test` to verify.\n")


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog="mailinbox.py",
        description="JARVIS v4 Mail-in-a-Box email client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # inbox
    p_inbox = sub.add_parser("inbox", help="List recent emails")
    p_inbox.add_argument("--count",  "-n", type=int, default=10, help="Number of emails to show (default: 10)")
    p_inbox.add_argument("--folder", "-f", default="",           help="Folder to read (default: INBOX)")
    p_inbox.set_defaults(func=cmd_inbox)

    # read
    p_read = sub.add_parser("read", help="Read a single email by UID")
    p_read.add_argument("uid",            help="Email UID (from inbox listing)")
    p_read.add_argument("--folder", "-f", default="", help="Folder (default: INBOX)")
    p_read.set_defaults(func=cmd_read)

    # send
    p_send = sub.add_parser("send", help="Send an email")
    p_send.add_argument("to",      help="Recipient email address")
    p_send.add_argument("subject", help="Subject line")
    p_send.add_argument("body",    help="Message body (plain text)")
    p_send.set_defaults(func=cmd_send)

    # folders
    p_folders = sub.add_parser("folders", help="List all IMAP folders")
    p_folders.set_defaults(func=cmd_folders)

    # test
    p_test = sub.add_parser("test", help="Verify IMAP + SMTP connectivity")
    p_test.set_defaults(func=cmd_test)

    # setup
    p_setup = sub.add_parser("setup", help="Interactive config wizard")
    p_setup.set_defaults(func=cmd_setup)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
