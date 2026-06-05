# Utilities — example.com

Shell scripts and Python tools this agent has built to make recurring tasks easy.
Add a new script here whenever you automate something useful.

## How to use

Run any utility from your agent working directory (`agents/example.com/`):

```bash
bash utilities/<script>.sh
# or
python3 utilities/<script>.py
```

Make scripts executable so they can run without the interpreter prefix:

```bash
chmod +x utilities/<script>.sh
./utilities/<script>.sh
```

---

## Index

| Script | Purpose | Usage |
|--------|---------|-------|
| *(agent adds entries here)* | | |

---

## Notes

- Keep scripts focused and single-purpose
- Add an entry to the Index table above whenever you add a script
- Scripts that run regularly should be promoted to `routines/`
- Document any credentials or env vars the script requires at the top of the file
