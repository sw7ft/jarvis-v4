#!/usr/bin/env python3
"""
browser.py — JARVIS v4 persistent browser app (Playwright + system Chrome).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 SETUP & CONFIG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 This is the MASTER copy at jarvisv4/apps/browser.py.
 deploy.py copies it to agents/<name>/apps/browser.py and
 injects per-agent constants (profile dir, CDP port, etc.)
 derived from the agent name.

 Each agent owns ONE persistent Chrome process backed by ONE
 on-disk profile (cookies, logins, extensions, history all stick).

 Strict 1:1 mapping:
   agent.name  ──► profile dir   ──► CDP port  ──► one Chrome PID

 Lifecycle (lazy, like mailinbox):
   * Deploy just lays the file down with derived defaults.
   * The first nav command auto-launches Chrome if not running.
   * Use `launch`/`stop` to manage explicitly (popover does this).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 USAGE (from agent working directory)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   python3 apps/browser.py launch [--headless]
   python3 apps/browser.py stop
   python3 apps/browser.py status
   python3 apps/browser.py test

   python3 apps/browser.py goto       <url>
   python3 apps/browser.py snapshot   [--format text|html] [--full]
   python3 apps/browser.py extract    <css-selector>
   python3 apps/browser.py click      <css-selector>
   python3 apps/browser.py type       <css-selector> <text>
   python3 apps/browser.py fill       <css-selector> <text>
   python3 apps/browser.py wait       <css-selector> [--timeout 10]
   python3 apps/browser.py screenshot [--path FILE] [--full-page]
   python3 apps/browser.py eval       '<js-expression>'
   python3 apps/browser.py tabs       [list|switch N|new [url]|close N]
   python3 apps/browser.py back  | forward | reload

   # Per-site knowledge files live in apps/browser-context/<domain>.md
   python3 apps/browser.py context list
   python3 apps/browser.py context show     <domain>
   python3 apps/browser.py context write    <domain> --text "..."   (or stdin)
   python3 apps/browser.py context append   <domain> --text "..."   (or stdin)
   python3 apps/browser.py context auto     [<domain>] [--url URL]
   python3 apps/browser.py context path     [<domain>]
   python3 apps/browser.py context rm       <domain>

 Add --json to ANY command for machine-parseable output.

 Requires:  pip install playwright   (Chromium not needed — uses system Chrome)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Injected by deploy.py for per-agent copies ──────────────────────────────
# Empty here in the master; deploy.py overwrites these on copy.
DEFAULT_PROFILE_NAME = ""   # e.g. "example.com"
DEFAULT_PROFILE_DIR  = ""   # absolute path, e.g. "/path/to/jarvisv4/agents/<name>/browser-profile"
DEFAULT_CDP_PORT     = 0    # deterministic per agent (9300-9999)
DEFAULT_CHROME_PATH  = ""   # absolute path to system Chrome; "" → auto-detect
DEFAULT_HEADLESS     = False
DEFAULT_START_URL    = "about:blank"
# ─────────────────────────────────────────────────────────────────────────────

META_NAME    = ".jarvis-meta.json"
LAUNCH_WAIT  = 30   # seconds to wait for CDP port to open after launch
NAV_TIMEOUT  = 30_000  # ms — passed to Playwright

# Per-agent site-knowledge directory. Lives next to this script (i.e.
# agents/<name>/apps/browser-context/) so each agent has its own corpus of
# "what I've learned about this site" markdown files, indexed by domain.
CONTEXT_DIR_NAME = "browser-context"


# ─── tiny output helpers ────────────────────────────────────────────────────

class Out:
    """Single output sink — toggles between human + JSON modes."""
    json_mode = False

    @classmethod
    def ok(cls, msg: str, **extra):
        if cls.json_mode:
            print(json.dumps({"ok": True, "msg": msg, **extra}))
        else:
            print(f"  \033[32m✓\033[0m {msg}")
            for k, v in extra.items():
                print(f"    {k}: {v}")

    @classmethod
    def info(cls, msg: str, **extra):
        if cls.json_mode:
            print(json.dumps({"ok": True, "info": msg, **extra}))
        else:
            print(f"  \033[36m▸\033[0m {msg}")
            for k, v in extra.items():
                print(f"    {k}: {v}")

    @classmethod
    def fail(cls, msg: str, code: int = 1, **extra):
        if cls.json_mode:
            print(json.dumps({"ok": False, "error": msg, **extra}))
        else:
            print(f"  \033[31m✗\033[0m {msg}", file=sys.stderr)
            for k, v in extra.items():
                print(f"    {k}: {v}", file=sys.stderr)
        sys.exit(code)

    @classmethod
    def data(cls, payload: dict):
        """Emit a structured result. JSON mode → dump dict. Text mode → pretty-print."""
        if cls.json_mode:
            print(json.dumps({"ok": True, **payload}))
        else:
            for k, v in payload.items():
                if isinstance(v, (dict, list)):
                    print(f"{k}:")
                    print(json.dumps(v, indent=2))
                else:
                    print(f"{k}: {v}")


# ─── config + meta ──────────────────────────────────────────────────────────

def _cfg() -> dict:
    name = DEFAULT_PROFILE_NAME
    profile = DEFAULT_PROFILE_DIR
    port = DEFAULT_CDP_PORT
    if not (name and profile and port):
        Out.fail(
            "browser.py is not configured. This is the master copy.\n"
            "Per-agent copies are created by deploy.py with constants injected."
        )
    return {
        "name":        name,
        "profile_dir": str(Path(profile).expanduser()),
        "cdp_port":    int(port),
        "chrome_path": DEFAULT_CHROME_PATH or _detect_chrome(),
        "headless":    bool(DEFAULT_HEADLESS),
        "start_url":   DEFAULT_START_URL or "about:blank",
    }


def _meta_path(cfg: dict) -> Path:
    return Path(cfg["profile_dir"]) / META_NAME


def _read_meta(cfg: dict) -> dict:
    p = _meta_path(cfg)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write_meta(cfg: dict, data: dict):
    p = _meta_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _clear_meta(cfg: dict):
    p = _meta_path(cfg)
    if p.exists():
        p.unlink()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── system helpers ─────────────────────────────────────────────────────────

def _detect_chrome() -> str:
    sysname = platform.system().lower()
    candidates: list[str] = []
    if "darwin" in sysname:
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif "linux" in sysname:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    elif "windows" in sysname:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return ""


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but we can't signal — still "alive"
    except Exception:
        return False


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _running(cfg: dict) -> tuple[bool, dict]:
    meta = _read_meta(cfg)
    if not meta:
        return False, {}
    if not _pid_alive(meta.get("pid")):
        return False, meta
    if not _port_open("127.0.0.1", int(meta.get("port") or cfg["cdp_port"])):
        return False, meta
    return True, meta


# ─── Chrome lifecycle ───────────────────────────────────────────────────────

def _launch_chrome(cfg: dict, headless: bool | None = None) -> dict:
    chrome = cfg["chrome_path"]
    if not chrome or not Path(chrome).is_file():
        Out.fail(
            "Google Chrome not found. Install Chrome or set DEFAULT_CHROME_PATH "
            "in this file to the absolute path of your Chrome binary."
        )
    profile = Path(cfg["profile_dir"])
    profile.mkdir(parents=True, exist_ok=True)
    port = int(cfg["cdp_port"])

    use_headless = bool(headless if headless is not None else cfg["headless"])

    args = [
        chrome,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-features=ChromeWhatsNewUI,GlobalMediaControls",
    ]
    if use_headless:
        args.append("--headless=new")

    log_path = profile / "chrome.log"
    proc = subprocess.Popen(
        args,
        stdout=open(log_path, "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from this process group → survives our exit
    )

    deadline = time.time() + LAUNCH_WAIT
    while time.time() < deadline:
        if _port_open("127.0.0.1", port):
            meta = {
                "pid":        proc.pid,
                "port":       port,
                "headless":   use_headless,
                "started_at": _now_iso(),
                "chrome":     chrome,
                "profile":    str(profile),
                "name":       cfg["name"],
            }
            _write_meta(cfg, meta)
            return meta
        if proc.poll() is not None:
            Out.fail(
                f"Chrome exited immediately with code {proc.returncode}. "
                f"See log: {log_path}"
            )
        time.sleep(0.25)

    try:
        os.kill(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    Out.fail(f"Chrome did not open CDP port {port} within {LAUNCH_WAIT}s")


def _stop_chrome(cfg: dict) -> tuple[bool, str]:
    meta = _read_meta(cfg)
    pid = meta.get("pid")
    if not pid or not _pid_alive(pid):
        _clear_meta(cfg)
        return False, "was not running"
    try:
        os.kill(int(pid), signal.SIGTERM)
    except Exception as e:
        return False, f"SIGTERM failed: {e}"
    for _ in range(20):
        time.sleep(0.15)
        if not _pid_alive(pid):
            break
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except Exception:
            pass
    _clear_meta(cfg)
    return True, f"killed pid {pid}"


def _ensure_running(cfg: dict) -> dict:
    running, meta = _running(cfg)
    if running:
        return meta
    return _launch_chrome(cfg)


# ─── Playwright session helpers ─────────────────────────────────────────────

def _pw_connect(cfg: dict):
    """Ensure Chrome is running, return (p, browser, ctx, page)."""
    meta = _ensure_running(cfg)
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        Out.fail(
            "Playwright is not installed.\n"
            "Install with:  pip3 install playwright\n"
            "(Chromium not required — we drive the system Chrome over CDP.)"
        )
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{meta['port']}")
    except Exception as e:
        p.stop()
        Out.fail(f"connect_over_cdp failed: {e}")

    if not browser.contexts:
        ctx = browser.new_context()
    else:
        ctx = browser.contexts[0]

    if not ctx.pages:
        page = ctx.new_page()
    else:
        page = ctx.pages[0]
    # Default timeouts (Playwright default of 30s is fine)
    page.set_default_navigation_timeout(NAV_TIMEOUT)
    page.set_default_timeout(NAV_TIMEOUT)
    return p, browser, ctx, page


def _pw_disconnect(p, browser):
    """Detach without killing Chrome. browser.close() on a CDP-attached browser
    only severs the connection — the underlying process keeps running."""
    try:
        browser.close()
    except Exception:
        pass
    try:
        p.stop()
    except Exception:
        pass


def _bump_last_used(cfg: dict, **fields):
    meta = _read_meta(cfg)
    if not meta:
        return
    meta["last_used"] = _now_iso()
    meta.update(fields)
    _write_meta(cfg, meta)


# ─── commands ───────────────────────────────────────────────────────────────

def cmd_launch(args):
    cfg = _cfg()
    running, meta = _running(cfg)
    if running:
        Out.ok("already running", pid=meta["pid"], port=meta["port"],
               headless=meta.get("headless", False))
        return
    meta = _launch_chrome(cfg, headless=args.headless)
    Out.ok("launched", pid=meta["pid"], port=meta["port"], headless=meta["headless"],
           profile=meta["profile"])


def cmd_stop(args):
    cfg = _cfg()
    stopped, msg = _stop_chrome(cfg)
    if stopped:
        Out.ok(msg)
    else:
        Out.info(msg)


def cmd_status(args):
    cfg = _cfg()
    running, meta = _running(cfg)
    payload = {
        "name":        cfg["name"],
        "running":     running,
        "profile_dir": cfg["profile_dir"],
        "port":        cfg["cdp_port"],
        "pid":         meta.get("pid"),
        "headless":    meta.get("headless"),
        "started_at":  meta.get("started_at"),
        "last_used":   meta.get("last_used"),
        "last_url":    meta.get("last_url"),
        "chrome_path": cfg["chrome_path"],
    }
    Out.data(payload)


def cmd_test(args):
    cfg = _cfg()
    if not cfg["chrome_path"] or not Path(cfg["chrome_path"]).is_file():
        Out.fail("Chrome binary not found", chrome_path=cfg["chrome_path"])

    p, browser, ctx, page = _pw_connect(cfg)
    try:
        url   = page.url
        title = page.title()
    finally:
        _pw_disconnect(p, browser)
    Out.ok("CDP attach OK", title=title, url=url, port=cfg["cdp_port"])


def cmd_goto(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        resp = page.goto(args.url, wait_until="domcontentloaded")
        # let a beat of network settle for SPAs without forcing networkidle
        try:
            page.wait_for_load_state("load", timeout=10_000)
        except Exception:
            pass
        final_url = page.url
        title     = page.title()
        status    = resp.status if resp else None
    finally:
        _pw_disconnect(p, browser)
    _bump_last_used(cfg, last_url=final_url)
    Out.data({"final_url": final_url, "title": title, "status": status})


def cmd_snapshot(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        url   = page.url
        title = page.title()
        if args.format == "html":
            body = page.content()
        else:
            body = page.evaluate("() => document.body ? document.body.innerText : ''")
            body = "\n".join(line.rstrip() for line in body.splitlines())
        if not args.full:
            limit = 8000 if args.format == "text" else 20000
            if len(body) > limit:
                body = body[:limit] + f"\n… (truncated; {len(body)-limit} more chars, pass --full for everything)"
    finally:
        _pw_disconnect(p, browser)
    _bump_last_used(cfg, last_url=url)
    if Out.json_mode:
        Out.data({"url": url, "title": title, "format": args.format, "body": body})
    else:
        print(f"URL:   {url}")
        print(f"Title: {title}")
        print("─" * 70)
        print(body)


def cmd_extract(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        loc = page.locator(args.selector)
        count = loc.count()
        if count == 0:
            results: list[str] = []
        else:
            results = [loc.nth(i).inner_text() for i in range(min(count, 50))]
    finally:
        _pw_disconnect(p, browser)
    if Out.json_mode:
        Out.data({"selector": args.selector, "count": count, "results": results})
    else:
        print(f"matches: {count}")
        for i, t in enumerate(results):
            print(f"[{i}] {t}")


def cmd_click(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.locator(args.selector).first.click(timeout=NAV_TIMEOUT)
        try:
            page.wait_for_load_state("load", timeout=5_000)
        except Exception:
            pass
        url   = page.url
        title = page.title()
    finally:
        _pw_disconnect(p, browser)
    _bump_last_used(cfg, last_url=url)
    Out.ok(f"clicked {args.selector}", url=url, title=title)


def cmd_type(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.locator(args.selector).first.type(args.text, delay=20)
    finally:
        _pw_disconnect(p, browser)
    Out.ok(f"typed into {args.selector}", chars=len(args.text))


def cmd_fill(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.locator(args.selector).first.fill(args.text)
    finally:
        _pw_disconnect(p, browser)
    Out.ok(f"filled {args.selector}", chars=len(args.text))


def cmd_wait(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.locator(args.selector).first.wait_for(timeout=int(args.timeout * 1000))
    except Exception as e:
        _pw_disconnect(p, browser)
        Out.fail(f"wait timed out: {e}")
        return
    _pw_disconnect(p, browser)
    Out.ok(f"selector appeared: {args.selector}")


def cmd_screenshot(args):
    cfg = _cfg()
    out_path = Path(args.path).expanduser() if args.path else (
        Path(cfg["profile_dir"]) / "screenshots" /
        f"screenshot-{int(time.time())}.png"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.screenshot(path=str(out_path), full_page=bool(args.full_page))
        url = page.url
    finally:
        _pw_disconnect(p, browser)
    _bump_last_used(cfg, last_url=url, last_screenshot=str(out_path))
    Out.ok("screenshot saved", path=str(out_path), url=url,
           bytes=out_path.stat().st_size)


def cmd_eval(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        # Auto-wrap a bare expression into an arrow function so users can write
        # both `document.title` and `() => document.title`.
        expr = args.js.strip()
        if not expr.startswith("(") and "=>" not in expr and not expr.startswith("function"):
            expr = f"() => ({expr})"
        result = page.evaluate(expr)
    finally:
        _pw_disconnect(p, browser)
    if Out.json_mode:
        try:
            print(json.dumps({"ok": True, "result": result}))
        except TypeError:
            print(json.dumps({"ok": True, "result": repr(result)}))
    else:
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2))
        else:
            print(result)


def cmd_tabs(args):
    cfg = _cfg()
    sub = args.tabs_cmd or "list"
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        pages = ctx.pages
        if sub == "list":
            rows = []
            for i, pg in enumerate(pages):
                rows.append({"index": i, "url": pg.url, "title": pg.title()})
            if Out.json_mode:
                Out.data({"tabs": rows})
            else:
                for r in rows:
                    print(f"[{r['index']}] {r['title']}\n    {r['url']}")
        elif sub == "new":
            new_page = ctx.new_page()
            if args.url:
                new_page.goto(args.url, wait_until="domcontentloaded")
            Out.ok("new tab", index=len(pages), url=new_page.url, title=new_page.title())
        elif sub == "switch":
            idx = int(args.n)
            if idx < 0 or idx >= len(pages):
                Out.fail(f"no tab at index {idx} (have {len(pages)})")
            pages[idx].bring_to_front()
            Out.ok(f"switched to tab {idx}", url=pages[idx].url, title=pages[idx].title())
        elif sub == "close":
            idx = int(args.n)
            if idx < 0 or idx >= len(pages):
                Out.fail(f"no tab at index {idx} (have {len(pages)})")
            pages[idx].close()
            Out.ok(f"closed tab {idx}")
        else:
            Out.fail(f"unknown tabs subcommand: {sub}")
    finally:
        _pw_disconnect(p, browser)


# ─── site-context helpers ───────────────────────────────────────────────────

def _context_dir() -> Path:
    """Resolve the agent's site-knowledge directory (auto-creates).

    Lives next to this script — e.g. agents/<name>/apps/browser-context/.
    Each agent has its own corpus, scoped to its own apps/ dir, so context
    written for one client's site never leaks to another agent."""
    d = Path(__file__).resolve().parent / CONTEXT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_domain(s: str) -> str:
    """Accept a domain, URL, or filename and return a clean filesystem-safe domain.

    Examples:
      'https://www.impactauto.ca/about'  →  'impactauto.ca'
      'impactauto.ca'                    →  'impactauto.ca'
      'IMPACTAUTO.CA.md'                 →  'impactauto.ca'
    """
    import re as _re
    from urllib.parse import urlparse
    s = (s or "").strip().lower()
    if not s:
        return ""
    if s.endswith(".md"):
        s = s[:-3]
    if "://" in s:
        s = urlparse(s).hostname or s
    if s.startswith("www."):
        s = s[4:]
    # Keep only chars that are safe in a filename
    s = _re.sub(r"[^a-z0-9._-]", "", s)
    return s


def _context_file(domain: str) -> Path:
    domain = _normalize_domain(domain)
    if not domain:
        Out.fail("empty or invalid domain")
    return _context_dir() / f"{domain}.md"


def _auto_context_payload(page) -> str:
    """Scrape a structural summary of the current page into markdown."""
    info = page.evaluate("""() => {
      const txt = (sel) => {
        const el = document.querySelector(sel);
        return el ? (el.innerText || el.textContent || '').trim() : '';
      };
      const attr = (sel, a) => {
        const el = document.querySelector(sel);
        return el ? (el.getAttribute(a) || '') : '';
      };
      const all = (sel, fn, limit) => {
        const out = [];
        document.querySelectorAll(sel).forEach((el) => {
          if (limit && out.length >= limit) return;
          const v = fn(el);
          if (v) out.push(v);
        });
        return out;
      };
      return {
        url:        location.href,
        title:      document.title || '',
        description: attr('meta[name="description"]', 'content') ||
                     attr('meta[property="og:description"]', 'content') || '',
        canonical:  attr('link[rel="canonical"]', 'href'),
        lang:       document.documentElement.lang || '',
        h1: all('h1', (e) => (e.innerText||'').trim(), 5),
        h2: all('h2', (e) => (e.innerText||'').trim(), 25),
        h3: all('h3', (e) => (e.innerText||'').trim(), 25),
        nav_links: all('nav a, header a', (e) => {
          const t = (e.innerText||'').trim();
          const h = e.href || '';
          return (t && h) ? `${t} → ${h}` : '';
        }, 30),
        forms: all('form', (f) => {
          const action = f.getAttribute('action') || '(no action)';
          const method = (f.getAttribute('method') || 'get').toLowerCase();
          const fields = [];
          f.querySelectorAll('input, textarea, select').forEach((i) => {
            const name = i.getAttribute('name') || i.getAttribute('id') || '';
            const type = i.getAttribute('type') || i.tagName.toLowerCase();
            if (name && type !== 'hidden') fields.push(`${type}:${name}`);
          });
          return `${method} ${action}  (${fields.join(', ')})`;
        }, 10),
      };
    }""")

    lines: list[str] = []
    domain = _normalize_domain(info.get("url", ""))
    lines.append(f"# {domain}")
    lines.append("")
    if info.get("title"):
        lines.append(f"**Title**: {info['title']}")
    if info.get("description"):
        lines.append(f"**Description**: {info['description']}")
    if info.get("canonical"):
        lines.append(f"**Canonical**: {info['canonical']}")
    if info.get("lang"):
        lines.append(f"**Language**: {info['lang']}")
    lines.append(f"**Sampled From**: {info.get('url','')}")
    lines.append(f"**Generated**: {_now_iso()}")
    lines.append("")

    def _section(title, items):
        if not items:
            return
        lines.append(f"## {title}")
        for it in items:
            lines.append(f"- {it}")
        lines.append("")

    _section("H1", info.get("h1") or [])
    _section("H2", info.get("h2") or [])
    _section("H3", info.get("h3") or [])
    _section("Navigation Links", info.get("nav_links") or [])
    _section("Forms", info.get("forms") or [])

    lines.append("## Notes")
    lines.append("<!-- Add hand-written notes below this line; `context auto` "
                 "never touches anything after this marker. -->")
    lines.append("")
    return "\n".join(lines)


_AUTO_NOTES_MARKER = "## Notes"


def _read_existing_notes(path: Path) -> str:
    """Return everything from the '## Notes' section onwards (preserves user edits)."""
    if not path.is_file():
        return ""
    text = path.read_text()
    idx  = text.find(_AUTO_NOTES_MARKER)
    return text[idx:] if idx >= 0 else ""


def cmd_context(args):
    sub = args.context_cmd or "list"
    cdir = _context_dir()

    if sub == "list":
        files = sorted(cdir.glob("*.md"))
        if Out.json_mode:
            out = []
            for f in files:
                try:
                    out.append({
                        "domain": f.stem,
                        "path":   str(f),
                        "bytes":  f.stat().st_size,
                        "mtime":  datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    })
                except Exception:
                    pass
            Out.data({"dir": str(cdir), "count": len(out), "files": out})
        else:
            if not files:
                print(f"(no context files in {cdir})")
                return
            print(f"\n{cdir}")
            print("─" * 60)
            for f in files:
                size = f.stat().st_size
                print(f"  {f.stem:<35} {size:>6} B   {f.name}")
            print()
        return

    if sub == "path":
        if args.domain:
            print(str(_context_file(args.domain)))
        else:
            print(str(cdir))
        return

    if sub == "show":
        if not args.domain:
            Out.fail("domain required: context show <domain>")
        f = _context_file(args.domain)
        if not f.is_file():
            Out.fail(f"no context file for {args.domain}", path=str(f))
        if Out.json_mode:
            Out.data({"domain": f.stem, "path": str(f), "body": f.read_text()})
        else:
            print(f.read_text())
        return

    if sub == "rm":
        if not args.domain:
            Out.fail("domain required: context rm <domain>")
        f = _context_file(args.domain)
        if not f.is_file():
            Out.info(f"already absent: {args.domain}")
            return
        f.unlink()
        Out.ok(f"removed {f.name}", path=str(f))
        return

    if sub in ("write", "append"):
        if not args.domain:
            Out.fail(f"domain required: context {sub} <domain>")
        f = _context_file(args.domain)
        body = args.text or ""
        if not body and not sys.stdin.isatty():
            body = sys.stdin.read()
        if not body:
            Out.fail("no content (pass --text \"...\" or pipe via stdin)")
        if sub == "write":
            f.write_text(body)
            Out.ok(f"wrote {f.name}", path=str(f), bytes=len(body))
        else:
            existing = f.read_text() if f.is_file() else ""
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            f.write_text(existing + sep + body + ("\n" if not body.endswith("\n") else ""))
            Out.ok(f"appended to {f.name}", path=str(f), added_bytes=len(body))
        return

    if sub == "auto":
        cfg = _cfg()
        p, browser, ctx, page = _pw_connect(cfg)
        try:
            if args.url:
                page.goto(args.url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("load", timeout=8_000)
                except Exception:
                    pass
            payload = _auto_context_payload(page)
            current_url = page.url
        finally:
            _pw_disconnect(p, browser)

        domain = _normalize_domain(args.domain or current_url)
        if not domain:
            Out.fail(f"could not derive a domain from URL {current_url!r}")
        f = _context_file(domain)
        # Preserve any '## Notes' section the user has hand-edited
        existing_notes = _read_existing_notes(f)
        if existing_notes:
            # Strip the auto-generated Notes stub off the new payload and
            # re-attach whatever was already in the file (user notes survive).
            cutoff = payload.find(_AUTO_NOTES_MARKER)
            if cutoff >= 0:
                payload = payload[:cutoff] + existing_notes
        f.write_text(payload)
        _bump_last_used(cfg, last_url=current_url)
        Out.ok(f"saved site context for {domain}",
               path=str(f), bytes=len(payload), source_url=current_url)
        return

    Out.fail(f"unknown context subcommand: {sub}")


def cmd_back(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.go_back(wait_until="domcontentloaded")
        url = page.url
    finally:
        _pw_disconnect(p, browser)
    _bump_last_used(cfg, last_url=url)
    Out.ok("went back", url=url)


def cmd_forward(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.go_forward(wait_until="domcontentloaded")
        url = page.url
    finally:
        _pw_disconnect(p, browser)
    _bump_last_used(cfg, last_url=url)
    Out.ok("went forward", url=url)


def cmd_reload(args):
    cfg = _cfg()
    p, browser, ctx, page = _pw_connect(cfg)
    try:
        page.reload(wait_until="domcontentloaded")
        url = page.url
    finally:
        _pw_disconnect(p, browser)
    _bump_last_used(cfg, last_url=url)
    Out.ok("reloaded", url=url)


# ─── CLI entry point ────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog="browser.py",
        description="JARVIS v4 persistent browser (Playwright + system Chrome)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="Emit JSON output (for programmatic use)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("launch", help="Start Chrome for this agent's profile")
    s.add_argument("--headless", action="store_true",
                   help="Launch headless (default: headed so first-time logins work)")
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("stop", help="Kill Chrome for this agent's profile")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("status", help="Show running state, port, pid, last url")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("test", help="Verify CDP connectivity (auto-launches Chrome if needed)")
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("goto", help="Navigate the primary tab to a URL")
    s.add_argument("url")
    s.set_defaults(func=cmd_goto)

    s = sub.add_parser("snapshot", help="Dump the current page text or HTML")
    s.add_argument("--format", choices=["text", "html"], default="text")
    s.add_argument("--full",   action="store_true", help="Don't truncate output")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("extract", help="innerText of all elements matching a CSS selector")
    s.add_argument("selector")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("click", help="Click the first element matching a selector")
    s.add_argument("selector")
    s.set_defaults(func=cmd_click)

    s = sub.add_parser("type", help="Type into a selector (appends; doesn't clear)")
    s.add_argument("selector")
    s.add_argument("text")
    s.set_defaults(func=cmd_type)

    s = sub.add_parser("fill", help="Clear and replace a field's value")
    s.add_argument("selector")
    s.add_argument("text")
    s.set_defaults(func=cmd_fill)

    s = sub.add_parser("wait", help="Wait for a selector to appear")
    s.add_argument("selector")
    s.add_argument("--timeout", type=float, default=10.0, help="Seconds (default 10)")
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("screenshot", help="Save a PNG of the current page")
    s.add_argument("--path", default="", help="Output file (default: profile_dir/screenshots/...)")
    s.add_argument("--full-page", action="store_true", help="Capture entire scroll height")
    s.set_defaults(func=cmd_screenshot)

    s = sub.add_parser("eval", help="Evaluate JS in the page context")
    s.add_argument("js", help="JS expression OR `() => ...` OR `function() { ... }`")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("tabs", help="List, open, switch, or close tabs")
    s.add_argument("tabs_cmd", nargs="?", default="list",
                   choices=["list", "new", "switch", "close"])
    s.add_argument("n", nargs="?", help="Tab index (for switch/close)")
    s.add_argument("--url", default="", help="URL for `tabs new`")
    s.set_defaults(func=cmd_tabs)

    sub.add_parser("back",    help="Navigate back").set_defaults(func=cmd_back)
    sub.add_parser("forward", help="Navigate forward").set_defaults(func=cmd_forward)
    sub.add_parser("reload",  help="Reload current page").set_defaults(func=cmd_reload)

    s = sub.add_parser("context",
                       help="Manage per-site knowledge files in apps/browser-context/")
    s.add_argument("context_cmd", nargs="?", default="list",
                   choices=["list", "show", "write", "append", "auto", "rm", "path"],
                   help="list (default) | show | write | append | auto | rm | path")
    s.add_argument("domain", nargs="?", default="",
                   help="Domain or URL — auto-normalized (e.g. 'impactauto.ca')")
    s.add_argument("--text", default="",
                   help="Content for write/append (or read from stdin)")
    s.add_argument("--url",  default="",
                   help="For `context auto`: navigate here first; otherwise use current page")
    s.set_defaults(func=cmd_context)

    args = p.parse_args()
    Out.json_mode = args.json
    args.func(args)


if __name__ == "__main__":
    main()
