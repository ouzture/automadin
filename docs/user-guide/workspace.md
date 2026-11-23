# Understand the workspace

The target selector at the top is shared across the application. The left
navigation divides work into these pages:

| Page | Purpose |
| --- | --- |
| Setup center | Check readiness and open the setup action for each target type |
| Flows | Create, edit, validate, save, and step-run automation flows |
| Flow debugger | Run a selected flow with live status and detailed logs |
| Vision lab | Capture a target, inspect coordinates, crop template images, and test matching or OCR |
| Android files | Browse and preview files on a connected Android device without modifying them |
| Runs | Quick-run flows, manage saved run setups, schedules, active runs, and history |
| Automation library | Rename, organize, import, export, and remove stored resources |

## Target selection

The current target determines what target-specific commands can do. Preflight
checks compare a flow with the selected target before execution and report
missing resources, incompatible commands, unresolved flow references, or setup
problems.

Selecting a target is not the same as connecting to it. A run creates and owns
the required session. Browser sessions and Android devices can also have profile,
availability, or lease constraints that are checked at launch.

## Menus

- **File** contains complete portable backup and database-only transfer actions.
- **View > Theme** changes the application theme.
- **Tools** opens Setup Center, Runtime Components, ADB/OCR/notification setup,
  browser profiles, and managed emulators.
- **Help > Diagnostics** builds a privacy-limited support report and provides
  separate access to raw logs.
