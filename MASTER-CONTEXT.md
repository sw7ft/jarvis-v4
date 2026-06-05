# JARVIS v4

You are an agent in the JARVIS v4 system. Read this entire file before acting.

## What This Is

JARVIS v4 runs autonomous AI agents — one per client / system / scope. Each
agent is fully isolated:

- Its own SSH host (`ssh <agent.name>`)
- Its own RocketChat channel (`#<agent.name>`)
- Its own working directory (`agents/<agent.name>/`)
- Its own copy of the RocketChat app (`agents/<agent.name>/apps/rocketchat.py`)
- Its own tmux session (`<agent.name>` with dots replaced by dashes)

A 1:1:1:1 mapping between the agent, its SSH host, its monitored channel, and
its app copy. No ambiguity.

## Directory Layout

```
jarvisv4/
├── MASTER-CONTEXT.md                ← this file
├── deploy.py                        ← `python3 deploy.py <agent.name>`
├── apps/
│   └── master-rocketchat.py         ← template + supervisor admin tool
├── templates/
│   └── agent-context.md             ← scaffold copied to each new agent
├── modules/                         ← deployable capability packages
│   ├── README.md                    ← module index
│   └── <module-name>/
│       ├── MODULE.md                ← deploy guide (read this first)
│       └── <module files>
└── agents/
    └── <agent.name>/
        ├── context.md               ← agent-specific context (rendered from template)
        ├── docs/                    ← deep reference knowledge (agent writes here)
        ├── logs/                    ← dispatch.log, monitor.log
        └── apps/
            └── rocketchat.py        ← per-agent copy with config injected at top
```

## Naming Convention

| Resource           | Value                                            |
|--------------------|--------------------------------------------------|
| Agent name         | `<agent.name>` (alphanumeric, dots, dashes)      |
| SSH host           | `ssh <agent.name>` (defined in `~/.ssh/config`)  |
| RocketChat channel | `#<agent.name>`                                  |
| Agent directory    | `agents/<agent.name>/`                           |
| tmux session       | `<agent.name>` with `.` replaced by `-`          |

The dash form is only for tmux because its `session:window.pane` target syntax
treats dots as separators. Everywhere else uses the dot form verbatim.

## Deploy

```bash
python3 deploy.py <agent.name>
```

Currently uses the **Cursor CLI agent only** for pane 1. What `deploy.py` does:

1. **Scaffold** `agents/<agent.name>/` with `context.md` rendered from
   `templates/agent-context.md`.
2. **Copy + customize** `apps/master-rocketchat.py` →
   `agents/<agent.name>/apps/rocketchat.py`, injecting `DEFAULT_CHANNEL`,
   `DEFAULT_USER`, `DEFAULT_INTERVAL`, `DEFAULT_WEBHOOK_URL`,
   `DEFAULT_TMUX_SESSION`, and `DEFAULT_SYSTEM_PROMPT` at the top of the file.
3. **Create the RocketChat channel** (`#<agent.name>`) if missing.
4. **Register an incoming webhook** for that channel and write the URL into
   the per-agent copy's `DEFAULT_WEBHOOK_URL`.
5. **Kill any existing tmux session** with the same name.
6. **Launch a new tmux session** with one window (`main`) and two panes
   (`base-index 1`, `pane-base-index 1` so the panes are 1 and 2):
   - Pane 1 — Cursor CLI agent: `cursor agent --yolo "read context.md"`
   - Pane 2 — RocketChat monitor for `#<agent.name>`
7. **Attach** to the session.

Re-running deploy for an existing agent kills its session and recreates it
from scratch.

## Runtime — Receive Flow

1. The pane 2 monitor polls `#<agent.name>` every `DEFAULT_INTERVAL` seconds.
2. When a new human message arrives, the monitor sends it into pane 1 via
   `tmux send-keys` (`<session>:main.1`).
3. The Cursor agent (pane 1) processes the message using this file plus its
   `context.md` and replies via the send flow below.

## Runtime — Send Flow

The agent posts replies by running, from `agents/<agent.name>/`:

```bash
python3 apps/rocketchat.py send "<message>"
```

The channel is hard-wired in the per-agent copy (`DEFAULT_CHANNEL`), so no
channel argument is required. To target a different channel or DM:

```bash
python3 apps/rocketchat.py send "#other-channel" "<message>"
python3 apps/rocketchat.py send "@username" "<message>"
```

## STOP Control Signal

Any one-word `STOP` / `HALT` / `ABORT` message (case-insensitive, optional
trailing punctuation) in an agent's channel sends `Ctrl-C` to the agent's
tmux pane 1 — interrupting whatever it's doing. The STOP message itself is
never forwarded to the agent. Multi-word messages like "please stop" or
"stop subscribing" flow through as normal prompts. See `docs/stop-signal.md`
for full mechanics, safety properties, and roll-out steps for monitor
processes after upgrading the master.

## Dispatch Prompt (Cursor agents)

When the monitor forwards an RC message to pane 1, it sends a plain-language
task prompt — **not** a copy-paste shell command with placeholders. Older
versions included a heredoc like `send ... "$(cat <<'EOF'\n<your reply>\nEOF\n)"`
which freshly booted agents sometimes ran literally, posting `<your reply>` to
the channel. The monitor also defers dispatch while pane 1 is still on its
initial `read context.md` bootstrap (up to 90s) so messages don't arrive
before context is loaded.

## Dispatch Log

Every per-agent `rocketchat.py` writes a JSON-Lines log to
`agents/<agent.name>/logs/dispatch.log` capturing both directions of every
round trip:

| Event       | When written                                             | Fields                                          |
|-------------|----------------------------------------------------------|-------------------------------------------------|
| `dispatch`  | Monitor sends a new RC message into pane 1               | `ts, agent, channel, sender, msg_id, text`      |
| `send`      | Agent invokes `rocketchat.py send "<reply>"`             | `ts, agent, channel, text`                      |
| `stop`      | User typed STOP/HALT/ABORT — Ctrl-C delivered to pane 1  | `ts, agent, channel, sender, msg_id, text`      |

Example:

```json
{"ts":"2026-04-20T16:42:01Z","event":"dispatch","agent":"matts.super.com","channel":"#matts.super.com","sender":"matt","msg_id":"abc123","text":"check the server"}
{"ts":"2026-04-20T16:42:18Z","event":"send","agent":"matts.super.com","channel":"#matts.super.com","text":"Server is up; uptime 12d."}
```

Quick reads:

```bash
tail -f agents/<agent.name>/logs/dispatch.log                                  # live
tail -f agents/<agent.name>/logs/dispatch.log | jq 'select(.event=="dispatch")' # inbound only
```

The supervisor's own copy of `master-rocketchat.py` writes to
`jarvisv4/logs/dispatch.log` for its admin operations.

## Supervisor Mode

A special agent named `supervisor` (deploy with `python3 deploy.py supervisor`)
manages other agents. Unlike client agents, the supervisor uses
`apps/master-rocketchat.py` directly for admin operations:

```bash
python3 apps/master-rocketchat.py setup        # one-time credential setup
python3 apps/master-rocketchat.py channels     # list channels
python3 apps/master-rocketchat.py users        # list users
python3 apps/master-rocketchat.py webhooks     # list webhooks
python3 apps/master-rocketchat.py send #ch msg # send as admin
```

The supervisor sees all sessions via `tmux ls`, can `tmux send-keys` into any
agent's pane 1 to delegate, and runs deploy/recall to spin agents up or down.

## Modules

Modules are self-contained packages of files that extend an agent's
capabilities for a specific function (e.g. a contact form handler, a newsletter
system, a cron monitor). They live in `jarvisv4/modules/` and are designed to
be deployed by an agent on demand — no changes to `deploy.py` needed.

### Directory Layout

```
jarvisv4/
└── modules/
    ├── README.md                  ← module index (list all modules here)
    └── <module-name>/
        ├── MODULE.md              ← single reference: what it does + deploy steps
        └── <files shipped to server or agent dir>
```

### How to Discover Modules

```bash
cat modules/README.md
```

This lists all available modules with a one-line description each.

### How to Deploy a Module

When asked to "deploy the `<name>` module":

1. Read its guide: `cat modules/<name>/MODULE.md`
2. Follow the deploy steps in that file — they are exact and self-contained.
3. The `MODULE.md` specifies what config is needed (SSH host, web root,
   webhook URL, etc.) and how to verify the deployment.

### Where Files Land

Most modules ship files to the **remote server** (website modules go into the
server's web root via `scp`). Some modules may also place files in the agent's
local directory (`agents/<agent.name>/`). `MODULE.md` always specifies this
explicitly in its **Files in this module** table.

### Rule

**When asked to deploy a module by name, always read
`modules/<module-name>/MODULE.md` first.** That file is the single source of
truth — do not guess or improvise the deploy steps.

## Rules for Agents

1. **Read before acting.** Always read `context.md` (and this file) before
   doing client work.
2. **Stay in scope.** Only work on your assigned `<agent.name>`. Use only
   `agents/<agent.name>/` for state, scripts, and logs.
3. **SSH naming.** Your server is `ssh <agent.name>`. Run `hostname && whoami`
   after connecting to verify.
4. **Reply via your own copy.** Always send through
   `agents/<agent.name>/apps/rocketchat.py`, never the master.
5. **Document as you go.** Append important findings, fixes, and procedures
   to your `context.md` so the next agent knows.

## Design Principles

- Each agent is a self-contained unit; killing its tmux session kills both
  panes (and their child processes) cleanly.
- The root `apps/master-rocketchat.py` is the **template + supervisor tool**
  only — it is never run as a client agent's monitor.
- Per-agent rocketchat.py copies have their config baked in at the top so
  the agent can reply with a single short command.
- Webhook registration happens at deploy time, not deferred.
