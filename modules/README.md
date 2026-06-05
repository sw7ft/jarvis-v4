# Jarvis v4 — Module Index

Modules are self-contained packages that extend an agent's capabilities for a
specific function. Each module lives in its own directory here and ships files
to the target server (or agent directory) when deployed.

## How to Use a Module

1. Find the module you need in the table below.
2. Read its `MODULE.md`: `cat modules/<module-name>/MODULE.md`
3. Follow the deploy steps in that file — they are exact and self-contained.

When asked to "deploy the `<name>` module", always read
`modules/<name>/MODULE.md` first — it is the single source of truth for that
module.

## Available Modules

| Module | Category | Description |
|--------|----------|-------------|
| `contact-form` | website | PHP contact/newsletter/chat/survey form handler — posts submissions to a RocketChat channel via incoming webhook, logs to file |

---

*To add a new module: create `modules/<module-name>/` and add a `MODULE.md`
following the standard format, then add a row to the table above.*
