#!/usr/bin/env bash
# audit-secrets.sh — scan repo for likely credential leaks before push
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FAIL=0
warn() { printf "  \033[33m⚠\033[0m  %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m  %s\n" "$1"; FAIL=1; }
ok()   { printf "  \033[32m✓\033[0m  %s\n" "$1"; }

echo ""
echo "JARVIS v4 — secret leak audit"
echo "─────────────────────────────"

# Patterns that often indicate real leaks (exclude docs with obvious placeholders)
PATTERNS=(
  'hunter2'
  'swiftapp\.ca'
  'swiftmedia\.ca'
  'aerochemcorp'
  '/Users/mp/'
  'hooks/[a-zA-Z0-9]{17,}/[a-zA-Z0-9]{17,}'
  'DEFAULT_PASSWORD\s*=\s*['\''"][^'\''"]{8,}['\''"]'
  'bot_password['\''"]?\s*:\s*['\''"][^'\''"]{4,}'
  'admin_password['\''"]?\s*:\s*['\''"][^'\''"]{4,}'
)

SKIP='\.git/|\.venv/|node_modules/'

for pat in "${PATTERNS[@]}"; do
  hits=$(rg -i --pcre2 "$pat" -g '!scripts/audit-secrets.sh' -g '!.git/**' "$ROOT" 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    fail "Pattern matched: $pat"
    echo "$hits" | head -5 | sed 's/^/       /'
    [[ $(echo "$hits" | wc -l) -gt 5 ]] && echo "       …"
  fi
done

# Tracked files under agents/ except _example scaffold
if git rev-parse --is-inside-work-tree &>/dev/null; then
  bad_agents=$(git ls-files 'agents/*' 2>/dev/null | rg -v '^agents/_example/' || true)
  if [[ -n "$bad_agents" ]]; then
    fail "Tracked files under agents/ (should only be _example scaffold):"
    echo "$bad_agents" | sed 's/^/       /'
  fi
fi

# Config files that must never be committed
for f in .env .env.local rocketchat/config.json; do
  if [[ -f "$f" ]]; then
    fail "Found $f — remove from repo and add to .gitignore"
  fi
done

echo ""
if [[ "$FAIL" -eq 0 ]]; then
  ok "No obvious leaks detected"
  echo ""
  echo "Reminder: run this before every push. False negatives are possible — review diffs manually."
  exit 0
else
  echo ""
  echo "Fix findings above before publishing."
  exit 1
fi
