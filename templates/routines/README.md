# Routines — {{name}}

Recurring tasks that run on a schedule or on demand. Each routine is a script
that does one job and reports back to `#{{channel}}`.

**NEVER use cron jobs.** Always run routines in the background of pane 2.
This way they stop automatically when `tmux kill-session` is run — no orphaned
processes, no system-wide side effects.

---

## How to start a routine

```bash
# Run in pane 2 background — output goes to logs/routines.log
tmux send-keys -t {{session}}:main.2 \
  "bash routines/<routine>.sh >> logs/routines.log 2>&1 &" Enter

# Check what's running in pane 2
tmux send-keys -t {{session}}:main.2 "jobs" Enter

# Watch the log
tail -f logs/routines.log
```

Why pane 2 and not cron:
- Routines die automatically when the agent session is killed
- All output goes to `logs/routines.log` — easy to inspect
- No system cron entries to clean up if an agent is removed

---

## Routine template

```bash
#!/usr/bin/env bash
# routines/<name>.sh
# Purpose: <what this does>
# Frequency: every <N> seconds/minutes
set -euo pipefail
cd "$(dirname "$0")/.."   # always run from agent working dir

CHANNEL="#{{channel}}"
INTERVAL=300   # seconds between runs

while true; do

  # --- your logic here ---

  # Report result to channel
  python3 apps/rocketchat.py send "$CHANNEL" "<result message>"

  sleep "$INTERVAL"
done
```

---

## Active Routines

| Routine | Purpose | Frequency | Started |
|---------|---------|-----------|---------|
| *(agent adds entries here)* | | | |

---

## Notes

- Always `cd` to the agent working dir at the top (`cd "$(dirname "$0")/.."`)
- Always post results back to `#{{channel}}` — don't fail silently
- Use a `while true; do ... sleep N; done` loop for repeating routines
- Stop a routine: `tmux send-keys -t {{session}}:main.2 "kill %<job#>" Enter`
- Promote a utility to a routine when you start running it on a schedule
