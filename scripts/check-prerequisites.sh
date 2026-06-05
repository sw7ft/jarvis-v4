#!/usr/bin/env bash
# check-prerequisites.sh — verify Mac/Linux host is ready for JARVIS v4 deploy
set -euo pipefail

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=1; }
warn() { printf "  \033[33m⚠\033[0m %s\n" "$1"; }

FAIL=0

echo ""
echo "JARVIS v4 — prerequisite check"
echo "──────────────────────────────"

# Python
if command -v python3 >/dev/null 2>&1; then
  ok "python3 $(python3 --version 2>&1 | awk '{print $2}')"
else
  fail "python3 not found"
fi

# tmux
if command -v tmux >/dev/null 2>&1; then
  ok "tmux $(tmux -V 2>&1 | awk '{print $2}')"
else
  fail "tmux not found — brew install tmux"
fi

# Cursor CLI
if command -v cursor >/dev/null 2>&1 && cursor agent --version >/dev/null 2>&1; then
  ok "cursor agent CLI"
else
  fail "cursor agent not in PATH — install from cursor.com"
fi

# venv / deps
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$ROOT/requirements.txt" ]]; then
  ok "requirements.txt present"
else
  fail "run from jarvisv4 repo root"
fi

# RC config
RC="$HOME/.config/rocketchat/config.json"
if [[ -f "$RC" ]]; then
  ok "Rocket.Chat config ($RC)"
else
  warn "Rocket.Chat not configured — run: python3 apps/master-rocketchat.py setup"
fi

# httpx (quick import test)
if python3 -c "import httpx, flask" 2>/dev/null; then
  ok "Python deps (httpx, flask)"
else
  warn "pip install -r requirements.txt"
fi

echo ""
if [[ "${FAIL:-0}" -eq 0 ]]; then
  echo "Ready to deploy. Next:"
  echo "  python3 deploy.py example.com --no-attach"
  echo "  python3 app.py"
  echo ""
  echo "Docs: docs/README.md"
  echo "Security: ./scripts/audit-secrets.sh (before git push)"
else
  echo "Fix failures above, then re-run this script."
  exit 1
fi
