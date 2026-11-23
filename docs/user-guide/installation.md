# Install and first setup

## Requirements

- Windows x64.
- Permission to install a per-user desktop application, or space to extract the
  portable ZIP.
- A supported browser runtime for browser flows.
- Optional: Android Platform Tools (ADB) for Android targets.
- Optional: a configured OCR engine for screen-text recognition.
- Optional: Pushover or Telegram credentials for notifications.

Automadin does not bundle browser binaries or Android Platform Tools. This keeps
the installer smaller and lets the Runtime Manager install only what you use.

## Install

Download `Automadin-Setup-<version>.exe` from the project release and run it. The
installed application stores writable data under `%LOCALAPPDATA%\Automadin`. The
uninstaller deliberately leaves that data in place.

The portable ZIP keeps its data in a `data` directory beside `Automadin.exe`.
Extract the complete ZIP before starting it; do not run the executable from
inside the archive.

## Complete Setup Center

On first start, open **Setup center** and choose the kind of automation you want:

- **Browser** — review detected browsers and install managed browser tools if required.
- **Android** — install or locate ADB, then connect a device or configure an emulator.
- **Windows** — available on Windows without a separate target runtime.

Readiness labels are intentionally precise. **Detected** means files were found;
**Launch tested** means Automadin successfully started an isolated browser test.
Neither status proves that a particular website, account, device, or application
will work until you test your flow.

Use **Tools > Runtime Components** to add or repair browser and Android runtimes.
Use **Help > Diagnostics** when a component remains unavailable.

## Protect your data

Create **File > Export Portable Backup** before moving an installation or making
major changes to its runtime setup. The backup includes the database and any
selected ready browser profiles. Treat the file as sensitive: profiles can
contain cookies and sign-in tokens, and the bundle is integrity-checked but not
encrypted.

Next: [Create your first flow](first-flow.md).
