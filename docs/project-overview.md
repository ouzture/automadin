---
project: automadin
type: architecture
status: active
last_reviewed: 2026-11-22
---

# Automadin project overview

Automadin is a Windows desktop application for visually building and running automation flows against Chromium, Android devices, and native Windows applications. The GUI is built with PySide6; Playwright, ADB, Win32 APIs, OpenCV, OCR, and SQLite provide the principal runtime integrations.

## Running the application

```powershell
python main.py
python main.py --debug
python main.py --data-dir <path>
```

Install development dependencies and run the primary checks with:

```powershell
python -m pip install -e ".[dev]"
python -m playwright install chromium
python -m pytest -q
python -m ruff check automadin tests main.py
```

Application data belongs in the selected data directory. Databases and browser profiles can contain sensitive state and must not be treated as source files.

## Architecture

### Models

`automadin/models/` contains serializable domain structures for flows, commands, typed values, parameters, hooks, schedules, run configurations, devices, locators, and visual rules. Keep these models independent of UI widgets where practical.

### Engines and capabilities

`automadin/engine/` owns target connectivity and low-level device behavior:

- `browser_engine.py` and related browser-session modules wrap Playwright.
- `adb_engine.py` and emulator modules handle Android targets.
- `windows_engine.py`, `windows_input.py`, and supporting services handle native Windows automation.

`automadin/capabilities/` contains higher-level target capabilities that can be composed by execution handlers.

### Execution

`automadin/execution/` turns a flow into runtime actions:

- `executor.py` walks command trees and dispatches registered handlers.
- `context.py` carries runtime state such as the active target, variables, and stop/pause coordination.
- `handlers/` contains command implementations grouped by capability.
- `build_executor.py` assembles the handler registry.
- `runner.py` and `run_coordinator.py` manage background execution and concurrent runs.

Browser sessions must be created and used on the owning worker thread because of Playwright's thread/greenlet constraints.

### Services

`automadin/services/` contains persistence and application services, including database access, profiles, schedules, portability, visual detection, OCR, screen capture, readiness checks, diagnostics, and runtime setup. Put reusable non-UI orchestration here rather than in dialogs or widgets.

### User interface

`automadin/ui/` contains the PySide6 application shell and feature views:

- `flow_editor/` — command palette, hierarchical editor, parameter editors, previews, and run-step controls.
- `launcher/` — target selection, run configurations, schedules, and live run panels.
- `flow_runner/` — execution status and logs.
- `management/` and `utility/` — resource management and supporting tools.
- `android_file_browser.py` — generic read-only browsing and previewing for connected Android devices.

Keep long-running or blocking work off the GUI thread and use the established Qt worker lifecycle utilities.

## Command system

Command metadata is defined in `automadin/resources/command_types.json`. Definitions include identifiers, labels, categories, hierarchy support, and parameter specifications.

When adding a command:

1. Add or update its metadata in `command_types.json`.
2. Implement the runtime behavior in the appropriate `automadin/execution/handlers/` module.
3. Register the handler through `automadin/execution/build_executor.py` or the established registry path.
4. Add focused tests for metadata compatibility, parameter handling, and runtime behavior.
5. Update UI presentation code only when the generic metadata-driven controls are insufficient.

Commands may contain `children` and `else_children`. Error behavior, retries, variables, typed values, and templates are runtime concerns shared across many handlers; reuse the central execution helpers instead of implementing local variants.

## Persistence and data safety

SQLite stores flows, run configurations, schedules, images, profiles, and related application state. Use `docs/database-schema.md` for schema work and preserve migration compatibility.

Portable backups can include browser profiles, cookies, and sign-in tokens. They are integrity-checked but are not a secrets-management mechanism. Never commit databases, profiles, generated logs, or exported backups.

## Sources of truth

- Code and tests define current behavior.
- `automadin/resources/command_types.json` defines command metadata.
- `pyproject.toml` defines the package version and Python package metadata.
- `docs/database-schema.md` documents persistence design.
- `docs/distribution.md` documents release and compliance procedures.
- `docs/index.md` lists the complete maintained documentation set.
