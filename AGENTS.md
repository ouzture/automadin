# AGENTS.md

## Project guidance

- Automadin is a Windows desktop automation application built with Python and PySide6.
- Use `docs/project-overview.md` for architecture or cross-cutting changes.
- Use `docs/database-schema.md` for persistence changes.
- Use `docs/distribution.md` for packaging, licensing, or release work.
- Read only the documentation relevant to the task; `docs/index.md` is the documentation map.

## Development runtime

- Prefer an active repository virtual environment when healthy; otherwise use a
  supported Python installation available through `python` or the Windows `py`
  launcher.

- Common checks:

```powershell
python -m pytest -q
python -m ruff check automadin tests main.py
```

## Installed application

- The user runs the installed Windows application; do not mirror source files to a separate execution copy.
- Source changes take effect for the user only after building and installing a newly requested package.

## Windows releases

- Increment the project version before every newly requested package.
- Use a verified Python 3.11 x64 environment. Resolve it without storing a
  developer-specific path:

```powershell
$releasePython = py -3.11 -c "import sys; print(sys.executable)"
```

- Verify and build with:

```powershell
& $releasePython tools\verify_release_environment.py requirements-release-windows.txt
pwsh -NoProfile -File tools\build_windows.ps1 -Python $releasePython -RequireInstaller
```

- Allow at least 10 minutes for Nuitka. The installer is written to `dist\installer\Automadin-Setup-<version>.exe`.
