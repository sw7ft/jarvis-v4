# STOP Signal — Aborting an In-Flight Agent

## What it does

Anyone in an agent's Rocket.Chat channel can type a one-word `STOP` (or `HALT`
/ `ABORT`) message and the agent gets a `Ctrl-C` delivered to its tmux pane
immediately — interrupting whatever it's doing, just like you'd press
`Ctrl-C` in a terminal.

The STOP message is **never forwarded to the agent**. It's a control signal,
not a prompt. The user can re-send their actual instruction afterwards once
the agent is idle again.

---

## How to trigger it

In the agent's RC channel, send a message whose entire trimmed body is one
of (case-insensitive, optional trailing `!`/`.`/`?`/`…`):

- `STOP`
- `HALT`
- `ABORT`

Panic-mashing also works: `STOP STOP STOP` triggers fine. Anything else,
including `please stop`, `stop the music`, or `STOPPING`, flows through to
the agent as a normal message.

---

## What happens, end-to-end

```
User types "STOP!" in #<agent.name>
       ↓
pane 2 monitor's next poll tick (≤ DEFAULT_INTERVAL seconds, default 10)
       ↓
monitor.py scans the unseen-message batch for any STOP match
       ↓ found one
1. Drain queue   — marks every unprocessed msg in this room "dispatched"
                   so nothing gets re-sent to the agent later
2. Clear hourglasses — strips any :hourglass_flowing_sand: reactions the
                       monitor was waiting to clean up
3. Ctrl-C × 2    — `tmux send-keys -t <session>:main.1 C-c` (200ms apart)
                   First tap kills typing/network reads; second tap aborts
                   in-flight Cursor tool calls
4. React 🛑      — :octagonal_sign: emoji on the user's STOP message
5. Ack           — bot posts "⛔ Stopped." back in the channel
6. Log           — appends {"event":"stop", sender, msg_id, text, ...} to
                   agents/<name>/logs/dispatch.log
```

The next poll tick proceeds normally — the agent is back at its idle Cursor
prompt and a fresh user message will dispatch as usual.

---

## Implementation

Lives in `apps/master-rocketchat.py`:

| Function | Purpose |
|---|---|
| `_STOP_WORDS` | Allowlist set: `{"STOP", "HALT", "ABORT"}` |
| `_is_stop_message(text)` | Returns True only when the *entire* trimmed message (minus trailing punctuation) is composed solely of STOP-words. Prevents "stop subscribing" from triggering. |
| `_send_ctrl_c(tmux_session, pane=1, taps=2, gap_sec=0.25)` | Fires Ctrl-C to a tmux pane N times. Safe at idle (just clears the input line). |
| STOP block in `monitor()` loop | Runs before the normal dispatch logic. Drains queue, sends Ctrl-C, reacts + acks. |

The matcher is deliberately strict — single-word, exact match against the
allowlist — so a website page-title or a quoted email containing the word
"stop" can never accidentally trigger a kill switch.

---

## Rolling the change out to existing agents

Every per-agent `rocketchat.py` is a frozen copy of the master at deploy time.
After updating `apps/master-rocketchat.py`, you must:

1. **Re-inject the file** for every agent — pulls the new code into
   `agents/<name>/apps/rocketchat.py`. Done in bulk via:

   ```python
   from deploy import copy_and_inject_rc, _read_existing_const, AGENTS_DIR
   # walk AGENTS_DIR, read each existing DEFAULT_*, re-inject from master
   ```

2. **Restart the monitor process** for each agent that's currently online —
   the long-running pane-2 monitor has the old code loaded in memory.
   Easiest: dashboard → RC popover → **Restart Monitor**.
   Or: `POST /api/rc/restart/<agent.name>`.

Offline agents pick up the new code automatically the next time they boot.

---

## Logged event format

A STOP shows up in `agents/<name>/logs/dispatch.log` as:

```json
{"ts":"2026-05-15T23:52:18Z","event":"stop","agent":"swifttech.ca","channel":"#swifttech.ca","sender":"matt","msg_id":"abc123","text":"STOP!"}
```

Distinct event-type so `jq 'select(.event=="stop")'` can pull every kill
event for an agent.

---

## Safety considerations

- **Bot messages are filtered out** before STOP detection (existing `ignore`
  set in the monitor). The agent quoting back "STOP" in its own reply
  cannot kill itself.
- **Ctrl-C at idle is a no-op** — just clears the input buffer. Safe to send
  even when the agent isn't actively working.
- **Race window**: if a STOP arrives in the same poll tick as a fresh
  user prompt, STOP wins. The user's prompt is silently dropped (not lost
  in the channel, just not forwarded). They can re-send.
- **Per-channel isolation**: STOP for `#agentA` does not affect `#agentB`.
  Each monitor watches one channel and only kills its own pane.

---

## Non-cursor (jarvis.py) mode

For agents using the legacy `jarvis.py` sub-agent dispatch (not Cursor),
STOP still drains the queue and acks in the channel, but it can't SIGINT
the in-flight subprocess from here safely. The current task finishes; the
queue is empty afterwards. In practice this only matters if you're still
running on the legacy path.
