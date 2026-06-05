# Contributing

Thank you for contributing to JARVIS v4 open source.

---

## What to contribute

- Bug fixes in `deploy.py`, `app.py`, master apps
- Documentation improvements in `docs/`
- New modules in `modules/<name>/` with complete `MODULE.md`
- Example agent improvements in `agents/_example/`

---

## What NOT to commit

- Real agent directories with client data
- Credentials, webhook URLs, passwords
- Browser profiles, dispatch logs
- Private hostnames or internal infrastructure details

Run `./scripts/audit-secrets.sh` before opening a PR.

---

## Development setup

```bash
git clone <repo> jarvisv4
cd jarvisv4
./scripts/check-prerequisites.sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 apps/master-rocketchat.py setup   # your RC instance
python3 deploy.py example.com --dry-run
python3 app.py
```

Use `agents/_example/` or a local-only agent name for testing.

---

## Code style

- Match existing Python style in the file you edit
- Minimal diffs — don't refactor unrelated code
- Master apps: keep `DEFAULT_*` injection points stable (deploy.py regex depends on them)
- Document behavior changes in `docs/`

---

## Pull requests

1. Describe the problem and solution
2. Note whether monitor restart / redeploy is required
3. Redact any RC URLs or credentials from logs in the PR description
4. Update docs if user-visible behavior changes

---

## Modules

New modules must include:

- `MODULE.md` with exact deploy steps
- Row in `modules/README.md`
- No hardcoded production URLs — use placeholders and config examples

---

## License

By contributing, you agree your contributions are licensed under the MIT
License in [LICENSE](../LICENSE).
