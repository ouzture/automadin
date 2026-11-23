# Contributing to Automadin

Automadin is a Windows desktop application built with Python and PySide6. Before
submitting a change, read the relevant maintained document in `docs/index.md`.

## Development setup

Use Python 3.11 or newer for development. Windows release builds use Python 3.11
and the exact dependency lock in `requirements-release-windows.txt`.

```powershell
python -m pip install -e ".[dev]"
python -m playwright install chromium
python -m pytest -q
python -m ruff check automadin tests main.py
```

Keep changes focused and add tests for changed behavior. Persistence changes must
preserve the migration and import-safety contract in `docs/database-schema.md`.
Packaging changes must follow `docs/distribution.md`.

## Protect user data

Do not commit databases, browser profiles, logs, exported backups, credentials,
tokens, private keys, machine-specific paths, or generated release artifacts.
Use synthetic values in tests and examples. Review diagnostics and logs before
sharing them because they may contain local or automation context.

Report security vulnerabilities privately as described in `SECURITY.md`.

## Contribution license

By submitting a contribution, you agree to license it under the project's
GNU General Public License, version 3 only (`GPL-3.0-only`), and certify that you
have the right to do so. Contributors retain copyright in their contributions.
