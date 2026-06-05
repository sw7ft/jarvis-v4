# Troubleshooting

Common issues and fixes when running JARVIS v4.

---

## Agent not replying in Rocket.Chat

| Check | Action |
|-------|--------|
| tmux session running? | `tmux ls` — expect `<agent-with-dashes>` |
| Monitor alive? | `pgrep -af "rocketchat.py monitor.*--tmux-session"` |
| Monitor log | `tail -f agents/<name>/logs/monitor.log` |
| Dispatch log | `tail -f agents/<name>/logs/dispatch.log` — see `dispatch` events? |
| RC credentials | `python3 apps/master-rocketchat.py channels` |
| Stale monitor code | Dashboard → RC popover → **Restart Monitor** after master upgrade |

Poll interval default is 10s — wait at least one interval after posting.

---

## Agent posted `<your reply>` or template text

**Cause:** Old dispatch prompt or message dispatched during `read context.md` boot.

**Fix:** Upgrade to latest `master-rocketchat.py`, refresh agent copy, restart
monitor. New prompts use plain-language instructions without heredoc placeholders
and defer dispatch while pane 1 is booting.

---

## STOP not working

Monitor must be restarted after STOP feature was added. Verify with:

```bash
grep _is_stop_message agents/<name>/apps/rocketchat.py
```

Only standalone `STOP`, `HALT`, `ABORT` trigger — not "please stop".

---

## Dashboard not loading (5112)

```bash
lsof -i :5112
python3 app.py
pip install flask flask-sock httpx
```

---

## deploy.py fails on RC login

```bash
python3 apps/master-rocketchat.py setup
chmod 600 ~/.config/rocketchat/config.json
```

Verify URL has no trailing slash issues and admin can create private groups.

---

## tmux pane 1 not Cursor

Redeploy:

```bash
python3 deploy.py <name> --no-attach
```

Ensure `cursor` is in PATH for the user running deploy.

---

## Mail app: connection failed

```bash
cd agents/<name>
python3 apps/mailinbox.py test
```

Check injected `DEFAULT_*` at top of `mailinbox.py`. Re-install via dashboard
with correct host/email/password.

---

## Browser app: CDP connection refused

```bash
cd agents/<name>
python3 apps/browser.py status
python3 apps/browser.py launch
```

- Install Google Chrome
- `pip install playwright`
- Only one Chrome per profile — check port collision if two agents share hash (rare)

---

## SSH from agent fails

```bash
ssh <agent.name> hostname
```

Fix `~/.ssh/config` on the JARVIS host. Agent sandbox allows SSH only to its
assigned host alias.

---

## Redeploy lost mail credentials

Redeploy without `--mailinbox-*` should **preserve** existing credentials from
the agent copy. If empty, re-enter via dashboard Add App → Mail → Save.

---

## Getting help

1. `logs/dispatch.log` + `logs/monitor.log` for the agent
2. Attach tmux: `tmux attach -t <session>`
3. [architecture.md](architecture.md) for flow understanding
4. Open a GitHub issue with logs redacted
