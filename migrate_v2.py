#!/usr/bin/env python3
"""
migrate_v2.py — Scaffold a v4 agent from a v2 agent.

v2 lives at the path in `JARVIS_V2_ROOT` and uses a different naming convention
(`client-ca`) plus a different per-agent config layout
(`utilities/website.conf` shell vars vs. v4's `apps/rocketchat.py` constants).

This module is pure-Python and Flask-free. It exposes:

    discover_v2_agents()        list of v2 agents that look migratable
    parse_website_conf(path)    KEY=VALUE -> dict (quotes stripped)
    suggest_v4_name(v2_name)    'a1heating-ca' -> 'a1heating.ca'
    build_plan(v2, v4, opts)    dry-run summary, returns dict
    execute_plan(plan)          generator yielding progress lines

The Flask layer in app.py wraps these for the dashboard.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ── Roots ────────────────────────────────────────────────────────────────────
# Optional: set JARVIS_V2_ROOT to the path of a legacy v2 install for migration.
V2_ROOT      = Path(os.environ.get("JARVIS_V2_ROOT", "")).expanduser() or None
V2_AGENTS    = (V2_ROOT / "agents") if V2_ROOT else None
V2_ARCHIVE   = (V2_ROOT / "archive") if V2_ROOT else None
V4_ROOT      = Path(__file__).resolve().parent
V4_AGENTS    = V4_ROOT / "agents"
V4_DEPLOY_PY = V4_ROOT / "deploy.py"
V4_CTX_TPL   = V4_ROOT / "templates" / "agent-context.md"

RC_BASE_URL  = os.environ.get("JARVIS_RC_URL", "https://your-rocketchat.example.com")

# v2 system / non-client agents we never want to migrate.
V2_SKIP_AGENTS = {"supervisor", "internal", "httpforms", "s1-docker"}

# Trailing TLD-like suffixes used to suggest dash->dot conversion.
TLD_SUFFIXES = {"ca", "com", "net", "io", "org", "app", "co", "dev", "ai", "us"}

# Files/dirs we never copy from v2 (logs, locks, OS junk, secrets).
SKIP_PATTERNS = {
    "*.log", "*.pid", "*.lock", "*.bak", "*.bak.*",
    "*.pyc", "__pycache__", ".DS_Store",
    "tmp",                      # v2 ephemerals
    ".rocketchat-map.json",     # never copy v2 RC creds
    "*.env",
}

# v2 utility scripts we explicitly drop because v4 has its own monitor / RC client.
V2_UTILITY_BLACKLIST = {
    "rc-website-tasks",
    "rc-website-tasks-context.md",
    "rc-website-tasks-last-seen",
    "rc-website-tasks.pid",
    "contact-webhook-config.php",   # may contain a webhook token
}


# ── Discovery ────────────────────────────────────────────────────────────────
def _safe_listdir(p: Path) -> list[Path]:
    try:
        return sorted(p.iterdir()) if p.is_dir() else []
    except OSError:
        return []


def discover_v2_agents() -> list[dict]:
    """Return migratable v2 agents with a small summary for the UI."""
    out: list[dict] = []
    if not V2_AGENTS or not V2_AGENTS.is_dir():
        return out

    for d in _safe_listdir(V2_AGENTS):
        if not d.is_dir() or d.name.startswith(".") or d.name in V2_SKIP_AGENTS:
            continue

        wc = d / "utilities" / "website.conf"
        ctx = d / "context.md"
        info = {
            "name":             d.name,
            "v4_suggested":     suggest_v4_name(d.name),
            "has_website_conf": wc.is_file(),
            "channel":          "",
            "ssh_host":         "",
            "ctx_size":         ctx.stat().st_size if ctx.is_file() else 0,
            "mtime":            int(d.stat().st_mtime),
            "v4_exists":        (V4_AGENTS / suggest_v4_name(d.name)).is_dir(),
        }
        if wc.is_file():
            try:
                conf = parse_website_conf(wc)
                info["channel"]  = (conf.get("RC_ROOM") or "").lstrip("#")
                info["ssh_host"] = (conf.get("WEBSITE_SSH") or "").replace("ssh ", "").strip()
            except Exception:
                pass
        out.append(info)

    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_website_conf(path: Path) -> dict[str, str]:
    """Parse a v2 website.conf (KEY=VALUE shell-ish), return clean dict.

    Handles single/double quoted values, strips inline `#` comments outside
    quoted strings, ignores blank/comment lines.
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
            continue
        val = val.strip()
        # Strip surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            # Strip inline comment for unquoted values
            val = val.split("#", 1)[0].strip()
        out[key] = val
    return out


# ── Naming ───────────────────────────────────────────────────────────────────
def suggest_v4_name(v2_name: str) -> str:
    """`client-ca` -> `client.ca`, `my-site-example-com` -> `my-site.example.com`.

    Rule: if the name ends in `-<TLD>` (TLD from the shortlist), the whole
    name is treated as a domain — every dash becomes a dot. Otherwise the
    name is left as-is and the human can edit it in the UI.
    """
    m = re.match(r"^(.+)-([a-z0-9]{2,5})$", v2_name)
    if m and m.group(2).lower() in TLD_SUFFIXES:
        return v2_name.replace("-", ".")
    return v2_name


# ── Plan + execute ───────────────────────────────────────────────────────────
def _matches_skip(name: str) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(name, pat) for pat in SKIP_PATTERNS)


def _safe_session_name(v4_name: str) -> str:
    return v4_name.replace(".", "-")


def _webhook_url_from_token(token: str) -> str:
    token = (token or "").strip().strip("/")
    if not token:
        return ""
    if token.startswith("http"):
        return token
    return f"{RC_BASE_URL}/hooks/{token}"


def _walk_for_copy(src_dir: Path, extra_blacklist: set[str] = None) -> list[Path]:
    """Return list of files under src_dir that pass SKIP_PATTERNS + blacklist."""
    extra_blacklist = extra_blacklist or set()
    files: list[Path] = []
    if not src_dir.is_dir():
        return files
    for root, dirs, fnames in os.walk(src_dir):
        # Filter dirs in-place so we don't descend into junk
        dirs[:] = [d for d in dirs if not _matches_skip(d) and d not in extra_blacklist]
        for f in fnames:
            if _matches_skip(f) or f in extra_blacklist:
                continue
            files.append(Path(root) / f)
    return files


def build_plan(v2_name: str, v4_name: str, options: dict | None = None) -> dict:
    """Validate inputs and return a structured migration plan dict.

    Schema:
        {
          ok: bool,
          errors: [str],
          warnings: [str],
          v2_name, v4_name,
          v2_dir, v4_dir,
          derived: {channel, webhook_url, interval, ssh_host, web_root, agent_cmd},
          options: {context, routines, utilities, docs, jobs_done},
          copy_summary: {context_md_bytes, routines_files, utilities_files,
                         docs_files, jobs_done_files, total_bytes}
        }
    """
    options = options or {}
    options = {
        "context":    bool(options.get("context",   True)),
        "routines":   bool(options.get("routines",  True)),
        "utilities":  bool(options.get("utilities", True)),
        "docs":       bool(options.get("docs",      True)),
        "jobs_done":  bool(options.get("jobs_done", True)),
        # Auto-archive the v2 source dir after a successful migration.
        # Defaults ON so the v2 agents/ folder is kept tidy as we cut over.
        "archive":    bool(options.get("archive",   True)),
    }

    errors: list[str] = []
    warnings: list[str] = []

    v2_name = (v2_name or "").strip()
    v4_name = (v4_name or "").strip()
    if not v2_name:
        errors.append("v2_name required")
    if not v4_name:
        errors.append("v4_name required")
    if v4_name and not re.match(r"^[a-zA-Z0-9._-]+$", v4_name):
        errors.append(f"v4_name '{v4_name}' has invalid characters")

    v2_dir = V2_AGENTS / v2_name
    v4_dir = V4_AGENTS / v4_name

    if v2_name and not v2_dir.is_dir():
        errors.append(f"v2 agent not found: {v2_dir}")
    if v4_name and v4_dir.exists():
        errors.append(f"v4 agent already exists: agents/{v4_name}/ (refusing to overwrite)")
    if v2_name in V2_SKIP_AGENTS:
        errors.append(f"'{v2_name}' is a v2 system agent — not migratable")

    derived: dict = {
        "channel": "", "webhook_url": "", "interval": 10,
        "ssh_host": v4_name, "web_root": "", "agent_cmd": "cursor",
    }

    if v2_dir.is_dir():
        wc = v2_dir / "utilities" / "website.conf"
        if not wc.is_file():
            warnings.append("no utilities/website.conf — RC channel + webhook will need manual setup after migration")
        else:
            conf = parse_website_conf(wc)
            channel = (conf.get("RC_ROOM") or "").lstrip("#").strip()
            if not channel:
                warnings.append("website.conf has no RC_ROOM — DEFAULT_CHANNEL will be empty")
                channel = v4_name
            derived["channel"]     = channel
            derived["webhook_url"] = _webhook_url_from_token(conf.get("WEBHOOK_TOKEN", ""))
            derived["interval"]    = int(conf.get("RC_POLL_INTERVAL") or 10)
            derived["ssh_host"]    = (conf.get("WEBSITE_SSH") or "").replace("ssh ", "").strip() or v4_name
            derived["web_root"]    = conf.get("WEBSITE_ROOT", "")
            derived["agent_cmd"]   = conf.get("AGENT_CMD", "cursor")
            if not derived["webhook_url"]:
                warnings.append("WEBHOOK_TOKEN missing in website.conf — replies will not post via webhook")

        # Conflict: another v4 agent already uses this channel?
        if derived["channel"]:
            for other in V4_AGENTS.iterdir() if V4_AGENTS.is_dir() else []:
                if not other.is_dir() or other.name == v4_name:
                    continue
                rc = other / "apps" / "rocketchat.py"
                if not rc.is_file():
                    continue
                m = re.search(r"^DEFAULT_CHANNEL\s*=\s*['\"]#?([^'\"]+)['\"]",
                              rc.read_text(errors="replace"), re.MULTILINE)
                if m and m.group(1).strip().lstrip("#") == derived["channel"]:
                    warnings.append(f"channel #{derived['channel']} already used by v4 agent '{other.name}'")
                    break

    # Archive destination (only meaningful when options['archive'] is True).
    # If the obvious destination already exists, the executor will append a
    # timestamp suffix; surface that intent here so the preview is honest.
    archive_dest_default = V2_ARCHIVE / v2_name
    archive_dest         = archive_dest_default
    archive_will_suffix  = False
    if options["archive"] and v2_dir.is_dir() and archive_dest_default.exists():
        archive_will_suffix = True
        archive_dest = V2_ARCHIVE / f"{v2_name}-<timestamp>"
        warnings.append(
            f"archive target {archive_dest_default} already exists — "
            f"v2 dir will be moved to {archive_dest_default}-<timestamp> instead"
        )

    # Copy summary (cheap walks)
    summary = {
        "context_md_bytes": 0, "routines_files": 0, "utilities_files": 0,
        "docs_files": 0, "jobs_done_files": 0, "total_bytes": 0,
        "archive_dest":    str(archive_dest) if options["archive"] else "",
        "archive_collision": archive_will_suffix,
    }
    if v2_dir.is_dir():
        ctx = v2_dir / "context.md"
        if ctx.is_file():
            summary["context_md_bytes"] = ctx.stat().st_size
        if options["routines"]:
            files = _walk_for_copy(v2_dir / "routines")
            summary["routines_files"] = len(files)
            summary["total_bytes"] += sum(f.stat().st_size for f in files)
        if options["utilities"]:
            files = _walk_for_copy(v2_dir / "utilities", V2_UTILITY_BLACKLIST)
            summary["utilities_files"] = len(files)
            summary["total_bytes"] += sum(f.stat().st_size for f in files)
        if options["docs"]:
            files = _walk_for_copy(v2_dir / "docs")
            summary["docs_files"] = len(files)
            summary["total_bytes"] += sum(f.stat().st_size for f in files)
        if options["jobs_done"]:
            files = _walk_for_copy(v2_dir / "jobs" / "done")
            summary["jobs_done_files"] = len(files)
            summary["total_bytes"] += sum(f.stat().st_size for f in files)
        if options["context"] and ctx.is_file():
            summary["total_bytes"] += summary["context_md_bytes"]

    return {
        "ok":           len(errors) == 0,
        "errors":       errors,
        "warnings":     warnings,
        "v2_name":      v2_name,
        "v4_name":      v4_name,
        "v2_dir":       str(v2_dir),
        "v4_dir":       str(v4_dir),
        "session":      _safe_session_name(v4_name),
        "derived":      derived,
        "options":      options,
        "copy_summary": summary,
    }


# ── Execution ────────────────────────────────────────────────────────────────
def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _render_v4_context(name: str, channel: str, ssh_host: str, session: str) -> str:
    """Render the v4 template with placeholders filled in."""
    if not V4_CTX_TPL.is_file():
        return f"# Agent: {name}\n\n(template missing — please populate context.md)\n"
    return (V4_CTX_TPL.read_text()
            .replace("{{name}}", name)
            .replace("{{channel}}", channel)
            .replace("{{ssh_host}}", ssh_host)
            .replace("{{session}}", session))


def _build_legacy_block(plan: dict, v2_ctx_text: str) -> str:
    d = plan["derived"]
    return (
        "\n\n---\n\n"
        "## Legacy v2 Notes\n\n"
        f"_Migrated from `{plan['v2_dir']}` on {_now_utc_str()}._\n"
        f"_Original v2 channel:_ `#{d.get('channel','')}`  "
        f"_SSH host:_ `{d.get('ssh_host','')}`  "
        f"_Web root:_ `{d.get('web_root','')}`\n\n"
        "<details><summary>Verbatim v2 context.md (preserved for reference)</summary>\n\n"
        f"{v2_ctx_text}\n\n"
        "</details>\n"
    )


def _append_history_entry(ctx_path: Path, plan: dict) -> None:
    """Append a History entry under the existing `## History` section."""
    if not ctx_path.is_file():
        return
    text = ctx_path.read_text()
    entry = f"- {_now_utc_str()} — Migrated from v2 agent `{plan['v2_name']}` via dashboard."
    if "## History" in text:
        # Insert right after the ## History heading + first explanatory paragraphs.
        # Simplest reliable approach: add the entry BEFORE the first existing bullet.
        new_text = re.sub(
            r"(## History\b.*?)(\n- )",
            lambda m: m.group(1) + f"\n{entry}" + m.group(2),
            text, count=1, flags=re.DOTALL,
        )
        if new_text == text:
            # No bullets yet — append at end of History section
            new_text = text.replace("## History", f"## History\n\n{entry}", 1)
        ctx_path.write_text(new_text)
    else:
        ctx_path.write_text(text + f"\n\n## History\n\n{entry}\n")


def _inject_rc_constants(rc_path: Path, plan: dict) -> None:
    """Rewrite DEFAULT_* constants in agents/<v4>/apps/rocketchat.py."""
    if not rc_path.is_file():
        raise RuntimeError(f"missing {rc_path} — deploy scaffold may have failed")
    text = rc_path.read_text()
    d    = plan["derived"]
    name = plan["v4_name"]

    def set_const(t: str, key: str, value: str) -> str:
        pat = re.compile(rf"^{key}\s*=\s*.*$", re.MULTILINE)
        return pat.sub(f"{key} = {value}", t, count=1) if pat.search(t) else t

    text = set_const(text, "DEFAULT_CHANNEL",      repr(f"#{d['channel']}"))
    text = set_const(text, "DEFAULT_USER",         repr(name))
    text = set_const(text, "DEFAULT_INTERVAL",     str(int(d["interval"])))
    if d["webhook_url"]:
        text = set_const(text, "DEFAULT_WEBHOOK_URL", repr(d["webhook_url"]))
    text = set_const(text, "DEFAULT_TMUX_SESSION", repr(plan["session"]))
    rc_path.write_text(text)


def _ensure_tag(agent_dir: Path, tag: str) -> None:
    tags_path = agent_dir / "tags.json"
    tags: list[str] = []
    if tags_path.is_file():
        try:
            data = json.loads(tags_path.read_text())
            if isinstance(data, list):
                tags = [str(t).strip().lower() for t in data if str(t).strip()]
        except Exception:
            tags = []
    if tag not in tags:
        tags.append(tag)
    tags_path.write_text(json.dumps(sorted(set(tags)), indent=2) + "\n")


def _selective_copy(src: Path, dst: Path, extra_blacklist: set[str] = None) -> tuple[int, int]:
    """Copy src/ -> dst/ honouring SKIP_PATTERNS + blacklist. Returns (files, bytes)."""
    extra_blacklist = extra_blacklist or set()
    if not src.is_dir():
        return (0, 0)
    dst.mkdir(parents=True, exist_ok=True)
    files = 0
    nbytes = 0
    for root, dirs, fnames in os.walk(src):
        dirs[:] = [d for d in dirs if not _matches_skip(d) and d not in extra_blacklist]
        rel_root = Path(root).relative_to(src)
        for f in fnames:
            if _matches_skip(f) or f in extra_blacklist:
                continue
            sp = Path(root) / f
            dp = dst / rel_root / f
            dp.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(sp, dp)
                files += 1
                nbytes += sp.stat().st_size
            except OSError:
                continue
    return (files, nbytes)


def execute_plan(plan: dict) -> Iterator[str]:
    """Run the migration. Yields human-readable progress lines."""
    if not plan.get("ok"):
        yield f"FAIL: plan not OK ({'; '.join(plan.get('errors', [])) or 'unknown'})"
        return

    v2_dir  = Path(plan["v2_dir"])
    v4_dir  = Path(plan["v4_dir"])
    v4_name = plan["v4_name"]
    opts    = plan["options"]
    d       = plan["derived"]

    # Step labels read "[N/M]" where M depends on whether the user opted
    # in to the post-migration archive step.
    M = 7 if opts.get("archive") else 6

    yield f"== Migrating v2:{plan['v2_name']} -> v4:{v4_name} =="
    yield f"   channel  : #{d['channel']}"
    yield f"   ssh host : {d['ssh_host']}"
    yield f"   webhook  : {(d['webhook_url'][:48] + '...') if d['webhook_url'] else '(none)'}"
    yield f"   interval : {d['interval']}s"
    yield f"   archive  : {'on (-> jarvisv2/archive/)' if opts.get('archive') else 'off'}"

    # 1. Scaffold via deploy.py with --no-channel --no-webhook --no-launch --no-attach
    yield ""
    yield f"[1/{M}] Scaffolding v4 agent skeleton (deploy.py --no-launch)..."
    cmd = ["python3", str(V4_DEPLOY_PY), v4_name,
           "--no-channel", "--no-webhook", "--no-launch", "--no-attach",
           "--interval", str(int(d["interval"]))]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(V4_ROOT))
    for line in (proc.stdout or "").splitlines():
        yield f"  {line}"
    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines():
            yield f"  ERR: {line}"
        yield f"FAIL: deploy.py exited rc={proc.returncode}"
        return

    if not v4_dir.is_dir():
        yield f"FAIL: deploy.py finished but {v4_dir} doesn't exist"
        return

    # 2. Inject RC constants into the freshly-scaffolded apps/rocketchat.py
    yield ""
    yield f"[2/{M}] Injecting RC constants into apps/rocketchat.py..."
    try:
        _inject_rc_constants(v4_dir / "apps" / "rocketchat.py", plan)
        yield f"  ok: DEFAULT_CHANNEL=#{d['channel']}, DEFAULT_USER={v4_name}"
        yield f"  ok: DEFAULT_TMUX_SESSION={plan['session']}, DEFAULT_INTERVAL={d['interval']}"
        if d["webhook_url"]:
            yield f"  ok: DEFAULT_WEBHOOK_URL set ({len(d['webhook_url'])} chars)"
        else:
            yield "  warn: no webhook URL — DEFAULT_WEBHOOK_URL left as scaffold default"
    except Exception as e:
        yield f"FAIL: rc inject error: {e}"
        return

    # 3. Build context.md: v4 template + appended Legacy v2 Notes
    yield ""
    yield f"[3/{M}] Building context.md (v4 template + Legacy v2 Notes)..."
    if opts["context"]:
        v2_ctx = v2_dir / "context.md"
        v2_ctx_text = v2_ctx.read_text(errors="replace") if v2_ctx.is_file() else "(no v2 context.md found)"
        rendered = _render_v4_context(v4_name, d["channel"], d["ssh_host"], plan["session"])
        legacy   = _build_legacy_block(plan, v2_ctx_text)
        (v4_dir / "context.md").write_text(rendered + legacy)
        yield f"  ok: context.md written ({len(rendered)} bytes template + {len(legacy)} bytes legacy)"
    else:
        yield "  skipped (option off)"

    # 4. Selective copytrees
    yield ""
    yield f"[4/{M}] Copying directories..."
    if opts["routines"]:
        n, b = _selective_copy(v2_dir / "routines", v4_dir / "routines")
        yield f"  routines/  -> routines/                 {n} files, {b} bytes"
    if opts["utilities"]:
        n, b = _selective_copy(v2_dir / "utilities", v4_dir / "utilities", V2_UTILITY_BLACKLIST)
        yield f"  utilities/ -> utilities/ (filtered)     {n} files, {b} bytes"
    if opts["docs"]:
        n, b = _selective_copy(v2_dir / "docs", v4_dir / "docs")
        yield f"  docs/      -> docs/                     {n} files, {b} bytes"
    if opts["jobs_done"]:
        n, b = _selective_copy(v2_dir / "jobs" / "done", v4_dir / "docs" / "legacy-v2-jobs")
        yield f"  jobs/done/ -> docs/legacy-v2-jobs/      {n} files, {b} bytes"

    # 5. Tag + history
    yield ""
    yield f"[5/{M}] Tagging + history entry..."
    try:
        _ensure_tag(v4_dir, "migrated-from-v2")
        yield "  ok: tag 'migrated-from-v2' added"
    except Exception as e:
        yield f"  warn: tag write failed: {e}"
    try:
        _append_history_entry(v4_dir / "context.md", plan)
        yield "  ok: history entry appended to context.md"
    except Exception as e:
        yield f"  warn: history append failed: {e}"

    # 6. Optional: archive the v2 source dir (mv into jarvisv2/archive/).
    #    This is the "auto archive" half of the dashboard's auto-recall +
    #    auto-archive flow — it keeps jarvisv2/agents/ tidy as we cut over.
    archive_done   = False
    archive_target = ""
    if opts.get("archive"):
        yield ""
        yield f"[6/{M}] Archiving v2 source -> jarvisv2/archive/..."
        try:
            V2_ARCHIVE.mkdir(parents=True, exist_ok=True)
            dest = V2_ARCHIVE / plan["v2_name"]
            if dest.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                dest = V2_ARCHIVE / f"{plan['v2_name']}-{stamp}"
                yield f"  warn: collision — using suffixed name {dest.name}"
            shutil.move(str(v2_dir), str(dest))
            archive_done   = True
            archive_target = str(dest)
            yield f"  ok: moved {v2_dir} -> {dest}"
        except Exception as e:
            yield f"  warn: archive failed (v4 agent is fine, v2 dir untouched): {e}"

    # Final: done banner + next steps
    yield ""
    yield f"[{M}/{M}] Done."
    yield ""
    yield f"SUCCESS: v4 agent scaffolded at agents/{v4_name}/"
    if archive_done:
        yield f"         v2 source archived to {archive_target}"
    yield "Next steps:"
    yield f"  1. Review agents/{v4_name}/apps/rocketchat.py constants"
    yield f"  2. Review agents/{v4_name}/context.md (template + Legacy v2 Notes appended)"
    yield f"  3. Click 'Restart' on the agent card to start tmux + monitor"


# ── CLI for ad-hoc testing ───────────────────────────────────────────────────
def _cli():
    import argparse
    p = argparse.ArgumentParser(description="Migrate a v2 JARVIS agent to v4.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List discoverable v2 agents")

    p_plan = sub.add_parser("plan", help="Print migration plan as JSON")
    p_plan.add_argument("v2_name")
    p_plan.add_argument("--target", default="", help="Target v4 name (default: auto-suggest)")

    p_run = sub.add_parser("run", help="Execute migration")
    p_run.add_argument("v2_name")
    p_run.add_argument("--target", default="")
    p_run.add_argument("--no-routines",  action="store_true")
    p_run.add_argument("--no-utilities", action="store_true")
    p_run.add_argument("--no-docs",      action="store_true")
    p_run.add_argument("--no-jobs",      action="store_true")
    p_run.add_argument("--no-context",   action="store_true")
    p_run.add_argument("--no-archive",   action="store_true",
                       help="Skip the post-migration mv to jarvisv2/archive/")

    args = p.parse_args()

    if args.cmd == "list":
        for a in discover_v2_agents():
            mark = "  [v4 EXISTS]" if a["v4_exists"] else ""
            print(f"  {a['name']:35s}  -> {a['v4_suggested']:35s}  ch=#{a['channel']:25s}{mark}")
        return

    target = args.target or suggest_v4_name(args.v2_name)

    if args.cmd == "plan":
        plan = build_plan(args.v2_name, target)
        print(json.dumps(plan, indent=2))
        return

    if args.cmd == "run":
        opts = {
            "context":   not args.no_context,
            "routines":  not args.no_routines,
            "utilities": not args.no_utilities,
            "docs":      not args.no_docs,
            "jobs_done": not args.no_jobs,
            "archive":   not args.no_archive,
        }
        plan = build_plan(args.v2_name, target, opts)
        if not plan["ok"]:
            print("ERRORS:")
            for e in plan["errors"]:
                print(f"  - {e}")
            sys.exit(2)
        for line in execute_plan(plan):
            print(line)


if __name__ == "__main__":
    _cli()
