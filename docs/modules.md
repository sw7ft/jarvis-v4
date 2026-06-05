# Modules

Modules are **optional capability packages** in `modules/<name>/`. They extend
what agents can deploy to remote servers (PHP handlers, server scripts, config
snippets) without modifying `deploy.py`.

**Full guide:** **[modules-system.md](modules-system.md)** — architecture, contact-form
deep dive, webhooks, authoring checklist.

**Apps (different concept):** **[apps-system.md](apps-system.md)** — local CLI tools in
`agents/<name>/apps/`.

---

## Quick index

```bash
cat modules/README.md
```

| Module | Description |
|--------|-------------|
| `contact-form` | PHP form handler → Rocket.Chat webhook + file log |

---

## How agents deploy modules

1. Read `modules/<name>/MODULE.md`
2. Gather SSH host, web root, webhook URL from agent context
3. Run the exact `scp` / `ssh` steps in MODULE.md
4. Verify (curl + check RC channel)
5. Document in agent's `context.md`

There is no central installer — `MODULE.md` is the runbook.

---

## Anatomy

```
modules/<module-name>/
├── MODULE.md           ← deploy steps (required)
├── <files to ship>
└── *.example           ← config templates for the server
```

Add a row to `modules/README.md` when you create a module.

See **[modules-system.md](modules-system.md)** for the contact-form walkthrough,
RC webhook wiring, spam checks, and how to add new modules.
