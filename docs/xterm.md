# xterm.js Terminal in JARVIS v4

## Overview

Each agent card has a **Term** button that opens a full interactive terminal inside the dashboard. It connects directly to the agent's tmux session (pane 1) via a WebSocket-backed PTY bridge, giving you real keyboard input, color output, and scrollback — identical to running `tmux attach-session` in a local terminal.

---

## Architecture

```
Browser (xterm.js)
    ↕  WebSocket  ws://localhost:5112/ws/tmux/<session>
Flask (flask-sock)
    ↕  PTY (pty.fork)
tmux attach-session -t <session>:main.1
    ↕
Agent pane (Cursor agent / shell)
```

### Key components

| Component | Role |
|-----------|------|
| `xterm.js 5.3` | Terminal emulator in the browser — renders VT100/ANSI sequences |
| `xterm-addon-fit` | Auto-resizes the terminal to fit its container on window resize |
| `xterm-addon-web-links` | Makes URLs in the output clickable |
| `flask-sock` | Adds WebSocket support to Flask (wraps `simple-websocket`) |
| `pty.fork()` | Forks a real PTY process so tmux sees a proper terminal |
| `TERM=xterm-256color` | Set in the child env so tmux enables full color + `clear` support |

---

## Backend — `/ws/tmux/<session>`

Defined in `app.py` via `@sock.route("/ws/tmux/<session>")`.

**Flow:**
1. `pty.fork()` creates a master/slave PTY pair and forks a child process.
2. Child execs `tmux attach-session -t <session>:main.1` with `TERM=xterm-256color` and `COLORTERM=truecolor` in the environment.
3. Parent enters a select loop:
   - Reads output from the PTY master fd → sends raw bytes to the WebSocket.
   - Receives messages from the WebSocket → writes to the PTY master fd (keystrokes).
4. Resize messages from the browser are prefixed with `\x01r<cols>,<rows>` and applied via `fcntl.ioctl(TIOCSWINSZ)`.

**Why PTY?** Without a PTY, tmux would detect it's not running in a real terminal and disable color, `clear`, cursor movement etc. `pty.fork()` gives it a proper terminal fd.

---

## Frontend — xterm.js

Loaded from CDN in `<head>`:
```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js"></script>
```

**`openTerm(name, session)`** — called when Term button is clicked:
1. Creates a `Terminal` instance with a dark GitHub-style theme.
2. Loads `FitAddon` and `WebLinksAddon`.
3. Calls `terminal.open(container)` then `fitAddon.fit()`.
4. Opens a WebSocket to `ws://localhost:5112/ws/tmux/<session>`.
5. `ws.onmessage` → `terminal.write(data)` (handles both binary `ArrayBuffer` and text).
6. `terminal.onData` → `ws.send(data)` (forwards keystrokes).
7. A `ResizeObserver` on the container calls `fitAddon.fit()` + sends a resize message whenever the modal is resized.

**`closeTerm()`** — disconnects WebSocket, disposes the terminal, clears the container.

**Resize protocol** — when `fitAddon.fit()` recalculates cols/rows, the browser sends:
```
\x01r<cols>,<rows>
```
The server detects the `\x01r` prefix and calls `TIOCSWINSZ` to update the PTY window size, so tmux reflows its layout.

---

## Terminal Theme

Matches the JARVIS v4 dashboard palette — GitHub Dark Dimmed base with blue cursor:

| Element | Color |
|---------|-------|
| Background | `#0d1117` |
| Foreground | `#c9d1d9` |
| Cursor | `#58a6ff` |
| Green | `#3fb950` |
| Red | `#ff7b72` |
| Yellow | `#d29922` |
| Blue | `#58a6ff` |
| Magenta | `#bc8cff` |

---

## Usage

- Click **Term** on any online agent card.
- The modal opens and immediately attaches to the agent's tmux pane 1.
- Type normally — keystrokes go directly to the agent session.
- **Esc** or **×** to close (disconnects WebSocket, tmux session remains running).
- The terminal auto-resizes when you resize the browser window.

---

## Dependencies

```
flask-sock>=0.7.0      # WebSocket support (pip install flask-sock)
simple-websocket       # Pulled in by flask-sock automatically
```

Both are already installed in the JARVIS v4 environment. No additional system dependencies needed — `pty`, `fcntl`, `select`, `struct`, `termios` are all Python stdlib.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "terminal does not support clear" | `TERM` not set in child env | Ensure `env["TERM"] = "xterm-256color"` before `execvpe` |
| Blank terminal, no output | Session name wrong or tmux not running | Check agent is online; verify `tmux ls` |
| Keystrokes not working | WebSocket not open yet | Wait for status to show "live" (green) |
| Colors look wrong | Browser cache of old xterm CSS | Hard refresh (`Cmd+Shift+R`) |
| Terminal too small / large | FitAddon not called after open | `closeTerm()` + reopen; or resize the browser window |
