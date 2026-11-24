# Windows distribution

Automadin is deployed as a Nuitka standalone directory through Qt's
`pyside6-deploy`. The same output is used for a portable ZIP and an Inno Setup
installer.

## Data safety

Release artifacts must not contain SQLite files, SQLite content hidden under a
different extension, Automadin backup/pending/rejected import files, or active and
staged `profiles`/`data` directories. They must also exclude browser binaries and
Android Platform Tools. `tools/verify_release.py` enforces this after every build.
It also requires a Windows GUI-subsystem `Automadin.exe`, the Qt `qwindows`
platform plugin and Qt Network DLL, a structurally plausible Playwright
`node.exe`, a plausible Playwright `cli.js`, and a valid
`release-manifest.json`. These deterministic checks catch incomplete build
outputs; they do not replace release signing, malware scanning, or execution on
a clean machine.

An installed build writes to `%LOCALAPPDATA%\Automadin`. A portable build is
identified by `portable.flag` and writes to `data` beside the executable.
`--data-dir PATH` and `AUTOMADIN_DATA_DIR` override both modes. The uninstaller
does not remove `%LOCALAPPDATA%\Automadin`.

**Help > Diagnostics** previews the exact text that **Copy Support Report** places
on the clipboard. That allowlist-based report contains app/platform/runtime
versions, aggregate workspace counts, and fixed Automadin log filenames, sizes,
and modified timestamps. It does not copy database records, workflow/profile/
device/account names, URLs, environment variables, local paths, or log messages.
Collection runs in a hidden, disposable helper process with a five-second
deadline. The UI can terminate that helper when the user cancels, so an
unresponsive database or filesystem check cannot keep the dialog alive
indefinitely. The parent accepts only a bounded, strictly validated aggregate
record and removes the private helper workspace before presenting the report.
**Open Logs Folder** and **Copy Logs Path** remain separate, explicit actions.
Raw logs and the local path can contain sensitive user, account, and automation
context, so review them before attaching or sharing them. Opening that same-user
data directory is a convenience action, not a security boundary against another
process already able to modify the user's Automadin files.

## Workspace portability and database-only backup

**File > Export Portable Backup** creates a versioned `.automadin-bundle`
containing a consistent SQLite snapshot and any ready managed browser profiles
the user selects. **File > Import Portable Backup** verifies the manifest,
database, archive paths, resource limits, and every file checksum before showing
the database counts and the action for each profile UUID.

A portable restore replaces the complete Automadin database on the next start;
it is not a merge. Existing profile directories are never overwritten, merged,
or deleted. An absent included UUID can be installed, valid matching local data
can be kept explicitly, and an unsafe or engine-mismatched collision blocks the
restore. Omitted profile records remain in the restored database but have no
portable browser data unless matching local storage is explicitly kept.

The restart activation is journalled and runs before the active database is
opened. Automadin publishes verified profile directories without replacement,
then activates the staged database through the validated database import path.
It first writes
`automadin-before-import-<timestamp>-<unique-id>.db`. A failed activation
compensates newly published profiles when safe and quarantines recovery material
instead of silently deleting it. Profile collisions reviewed as absent,
identical, or explicitly kept are checked again at restart so intervening local
changes cannot silently alter the restore decision.

Portable backups may contain cookies, tokens, history, and signed-in browser
state. Version 1 is checksummed but not encrypted or authenticated. Browser
secrets may be protected by the source Windows account or machine, so users
must expect to verify or repeat sign-in after a restore. The format and collision
contract are enforced by the portability services and their tests.

**File > Export Database Only** and **File > Import Database Only** remain
available for deliberately transferring flows, images, run setups, settings, and
profile records without browser-profile folders. Database-only import validates
SQLite integrity and the Automadin schema read-only, stages a consistent copy,
and applies it on the next start. Invalid pending imports are quarantined as
`automadin-import-rejected-<timestamp>.db`.

## Build prerequisites

- Windows x64
- Python 3.11 x64
- The exact packages in `requirements-release-windows.txt`, including
  `pyside6-deploy`, Nuitka, Playwright, and pywin32
- Internet access for the first Nuitka build
- Inno Setup 6.7 or newer when an installer is required
  (optional for standalone/portable builds)

Prepare and identify the release interpreter:

```powershell
py -3.11 -m pip install -r requirements-release-windows.txt
$releasePython = py -3.11 -c "import sys; print(sys.executable)"
```

Build the standalone folder and portable ZIP. When `ISCC.exe` is available on
`PATH` or in a standard Inno Setup 7 or 6 installation directory, the same command
also builds the installer:

```powershell
pwsh -File tools/build_windows.ps1 -Python $releasePython
```

The script first verifies Windows x64, Python 3.11, and every exact release
dependency pin. It reads the artifact version from `[project].version` in
`pyproject.toml`, compiles Automadin, rejects personal data
and redistributed runtime binaries, and creates
`dist/Automadin-Portable-<version>.zip` using ZIP64.

`pysidedeploy.spec` is a machine-neutral template. The build creates a temporary
copy containing the selected interpreter, project path, and version, passes that
copy to `pyside6-deploy`, and removes it afterward. Developer-local paths are not
stored in the checked-in specification.

A normal compile clears only the expected `dist/Automadin.dist` deployment folder
before invoking `pyside6-deploy`, then requires the executable to appear in that
exact folder. It never searches `dist` recursively or falls back to an older build.
`-SkipCompile` deliberately reuses only
`dist/Automadin.dist/Automadin.exe`; it fails when that explicit output is absent
or its Windows FileVersion/ProductVersion do not match the canonical project
version.

Installer behavior is explicit:

- If Inno Setup is discovered, installer compilation is attempted and any compiler
  failure fails the build.
- If it is absent, the default build prints an `Installer skipped` warning and
  still produces standalone/portable artifacts.
- `-RequireInstaller` makes a missing compiler a build failure and is recommended
  for release automation.
- `-SkipInstaller` deliberately omits installer compilation.
- `-InnoCompiler C:\path\to\ISCC.exe` selects a non-standard installation; an
  invalid explicit path is always an error.

`-SkipInstaller` cannot be combined with `-RequireInstaller` or `-InnoCompiler`.

## Build outputs and automatic gates

For the canonical project version, a complete release build produces:

- `dist/Automadin.dist/` — standalone application directory;
- `dist/Automadin-Portable-<version>.zip` — standalone files plus `portable.flag`;
- `dist/installer/Automadin-Setup-<version>.exe` — per-user installer when Inno
  Setup is available or required;
- `dist/source/Automadin-<version>/` — the application, Qt, and Qt for Python
  source archives that must be published beside every public binary release.

Before creating the portable archive or installer, the build writes
`release-manifest.json` with the application version, Python version, and the
Nuitka, PySide6, Playwright, and pywin32 versions. The static verifier then checks
the manifest, privacy exclusions, required Qt and Playwright files, PE structure,
and the GUI subsystem. Packaged Setup and portability screens use this manifest
as their authoritative application version when Python distribution metadata is
not installed.

The same stage generates three deterministic, offline compliance outputs:

- `release-sbom.cdx.json`, a CycloneDX 1.6 inventory of every exact distribution
  in `requirements-release-windows.txt`;
- `THIRD-PARTY-NOTICES.txt`, a human-readable package/version/license/evidence
  index;
- `third-party-licenses/manifest.json` plus byte-for-byte license and notice
  evidence copied from the installed distributions;
- `native-release-inventory.json`, which classifies and hashes every shipped
  `.exe`, `.dll`, and `.pyd`, plus exact Inno Setup compiler/license evidence;
- `corresponding-source/`, containing the verified source manifest and the
  source-availability notice installed with Automadin.

`release-license-policy.json` records the reviewed SPDX expression, runtime/build
scope, and evidence requirement for every locked distribution. Generation fails
when the lock and policy differ, an installed version drifts, package metadata
does not provide an unambiguous matching declaration, a declared license file is
missing, or required evidence cannot be copied as non-empty UTF-8 text. Every
evidence file and top-level compliance artifact is SHA-256 hashed; the release
verifier rejects missing, changed, untracked, or cross-document-inconsistent
files. During a supported source release build it also reconstructs the complete
component/evidence model from the exact installed lock and compares the generated
documents byte-for-byte with the checked-in lock, policy, and project metadata.
Artifact-only verification without those trusted source inputs remains a
structural integrity check; it is not allowed to satisfy compliance readiness.
The version-2 license manifest labels both trusted input records with
`canonicalization: utf-8-lf`. Lock and policy identities hash strict UTF-8 text
after CRLF and lone CR newlines are represented as LF. This preserves the same
identity across standard Windows and Unix checkouts while every other text byte,
including whitespace and final-newline presence, remains significant.

The CycloneDX SBOM's declared scope remains **exact locked Python
distributions**. Native payload coverage is deliberately separate and
file-exact: `release-native-policy.json` must classify every native file exactly
once, and generation records its size and SHA-256. Unknown, ambiguous, missing,
or changed files fail the build. The same inventory identifies the Inno Setup
compiler and preserves its exact license text.

Automadin selects the GPL-3.0-only route offered by Qt/PySide. Qt/PySide Corresponding Source
delivery is enforced for a public build:
`release-source-policy.json` pins the official Qt 6.10.2 and Qt for Python 6.10.2
source archives by URL, byte size, and SHA-256. `tools/release_sources.py` also
creates Automadin's exact source archive from a clean Git commit, including the
locked dependencies and build/install scripts. Upstream URLs establish
provenance only: all three verified archives must remain downloadable at no
charge from the same release location as the binaries.

Automadin declares `GPL-3.0-only` in the trusted release policy and ships the
canonical project-root `LICENSE`. The generator copies and hashes those exact
license bytes into release evidence. The source and native/installer blockers are
cleared only when their actual artifacts are regenerated and independently
verified against the checked-in policies. Normal installer builds remain
available for internal testing.

`-RequireComplianceReady` is the separate fail-closed legal-metadata gate. It
requires the trusted project, lock, and policy inputs and derives the expected
component inventory and blocker set from them; removing a blocker or dependency
from generated artifacts cannot make the gate pass. Any trusted publication
blocker fails verification:

```powershell
pwsh -File tools/build_windows.ps1 `
  -Python $releasePython `
  -RequireInstaller `
  -DownloadCorrespondingSource `
  -RequireComplianceReady
```

The first compliance-ready build downloads about 1.33 GB of official Qt source
archives. Later builds reuse them only after checking the pinned sizes and
SHA-256 values. Do not publish an installer or portable ZIP without publishing
the matching `dist/source/Automadin-<version>/` files alongside it.

This flag is intentionally independent of `-RequireInstaller`. Every public
binary release must use both gates. Authenticode signing is preferred and must be
verified when present. While signing is pending, an unsigned release is permitted
only when its release page prominently identifies that status, warns about
Windows publisher/SmartScreen prompts, and publishes SHA-256 hashes with the
matching Corresponding Source. Compliance readiness alone does not establish
publisher identity.

The frozen executable must also pass
`--release-browser-health-self-test <temporary-parent>`. This starts the packaged
browser-health worker through the same executable with an intentionally empty
browser location. Success means Chromium is reported unavailable at the launch
stage and both the worker workspace and outer scratch directory are removed. This
gate exercises frozen worker re-entry, Playwright's packaged driver, pywin32 Job
Object containment, and cleanup without touching normal application data.

The same build also runs
`--release-diagnostics-self-test <temporary-parent>`. It opens the packaged
Diagnostics widget offscreen against a disposable database and sensitive fixture
values, starts the packaged support-report helper through the same executable,
exercises its bounded strict protocol and the exact preview/copy path without
changing the system clipboard, rejects leaked record/log/path content, requires
the real packaged PySide6 version, and removes the complete scratch workspace.
Both packaged
self-tests run with a 60-second limit inside a kill-on-close Job Object and receive
an isolated fallback data directory. Diagnostics must additionally write an exact
nonce-bound success marker, so an older executable that ignores the new switch
cannot enter normal mode and falsely pass.

## Runtime Manager

The installed application opens Runtime Manager as a separate process through
**Tools > Runtime Components**.

- Playwright's own installer downloads Chromium, Firefox, and WebKit into a
  directory scoped to the packaged Playwright version.
- ADB is downloaded directly from Google's official URL only after the user
  accepts the Android SDK terms. Automadin extracts only `adb.exe`, its two
  required Windows DLLs, `NOTICE.txt`, and `source.properties`.
- Downloads stage under `runtime/.staging` and are activated only after
  validation. Failed or cancelled installs do not become current.
- Installed mode uses `%LOCALAPPDATA%\Automadin\runtime`; portable mode uses
  `data\runtime` beside the portable executable.

Runtime Manager reports two states separately: whether the managed package is
installed, and whether Automadin detected a usable active component. Browser
readiness requires the exact Chromium, Firefox, and WebKit revisions expected by
the packaged Playwright driver, their completion markers, and their Windows
executable payloads; this check does not launch the browsers. Incomplete managed
payloads are shown with **Repair** guidance. ADB readiness runs a bounded hidden
`adb version` probe and displays the active executable and reported version.

**Tools > Browser Profiles** reports runtime presence separately from profile-data
integrity. Chromium and Firefox profiles use the managed Playwright payload;
Chrome and Edge profiles require their exact installed system channel. Runtime
Manager does not install vendor Chrome or Edge, and no presence result claims that
a profile is signed in or that a future launch will succeed.

Valid explicit `PLAYWRIGHT_BROWSERS_PATH` and `ADB_PATH` values remain active and
are identified as external in Runtime Manager. Stale or non-working overrides are
ignored and replaced when a healthy bundled or managed component exists.

## Installer

The build script is the supported way to compile `packaging/Automadin.iss`, because
it injects the version read from `pyproject.toml` and the verified standalone source
directory. For a manual compilation, define `MyAppVersion`; the script deliberately
has no duplicated fallback version:

```powershell
$version = & python tools/project_metadata.py --version
ISCC.exe "/DMyAppVersion=$version" packaging/Automadin.iss
```

The installer is per-user and places application files under
`%LOCALAPPDATA%\Programs\Automadin` without administrator elevation. It deliberately
leaves per-user data intact during uninstall.

Every verified standalone now contains `install-files.manifest`, an exact,
case-collision-checked list of files owned by the installer. On upgrade, Setup
compares the previous and incoming manifests and removes only obsolete
installer-managed files. The previous manifest is accepted only when its exact
compile-time SHA-256 was retained by the preceding successful Setup; a missing,
edited, or otherwise mismatched manifest disables cleanup and preserves existing
files. Unknown files and protected `data`, `profiles`, `runtime`, logs, portable
markers, databases, and uninstaller metadata are never cleanup targets. Cleanup
also refuses an application root or parent path carrying the NTFS reparse-point
attribute. A deletion that cannot complete is preserved rather than being assigned
to the incoming release.

The retained SHA-256 is a corruption/stale-state guard, not a security boundary
against a process already able to alter both the user's installation directory and
that user's uninstall registry metadata. Code signing remains the separate
publisher/tamper control.

Each installer is a complete payload, so its successful install overwrites the
prior uninstall log with current-version ownership. This prevents an old release
from deleting a user file later recreated at a formerly managed path. A
file-to-directory or directory-to-file transition between manifests is rejected
before extraction with uninstall-first guidance; downgrades are also rejected
before installation begins.

`tools/smoke_windows_installer.py` compiles tiny test-mode installers with a unique
non-production identity, installs v1, upgrades to v2, rejects a shape-changing
upgrade and downgrade, and uninstalls. It proves obsolete managed-file cleanup,
tampered-manifest fail-preservation, junction-target preservation, survival of a
user file recreated at an obsolete path, protected/unknown data retention,
registry isolation, process-tree containment, and exact scratch restoration
without touching the production AppId or installed Automadin.

On an empty GitHub Actions runner, a separate strongly guarded mode installs and
uninstalls the exact production artifact into the runner scratch directory. It
requires the uninstall registry version to match the installed executable's fixed
file and product versions, then launches that exact executable with isolated data
and exercises normal second-instance activation before uninstalling it. It refuses
to run outside GitHub Actions or when the production uninstall identity already
exists. The Windows release CI job runs both modes.

## Release checks

Before publishing:

1. Run the full test suite and repository-wide Ruff with the release interpreter.
2. Run `tools/build_windows.ps1 -Python $releasePython -RequireInstaller
   -DownloadCorrespondingSource -RequireComplianceReady`; this
   performs the environment, static artifact, and packaged browser-health gates.
3. Confirm the standalone, portable ZIP, installer, and release manifest all
   report the expected version and publish their SHA-256 hashes.
4. Launch the frozen application and Runtime Manager with a temporary
   `--data-dir`; verify a second normal launch activates the first instance.
5. Install and launch Chromium, Firefox, and WebKit from that managed directory.
6. Accept the SDK terms, install ADB, and run the managed `adb.exe version`.
7. Cancel one download and confirm no partial component is activated.
8. Export and restore a database-only backup on an empty temporary data directory.
9. Export and restore a bundle with one managed browser profile; verify collision
   prompts, restart activation, the safety backup, and re-login guidance.
10. Open **Help > Diagnostics**; verify its preview matches the clipboard exactly,
    fixed log metadata contains no log messages or local paths, raw logs remain a
    separate warned action, and the frozen build reports its real PySide6 version.
11. On a disposable clean Windows VM, test first install, launch, Runtime Manager,
    first-run Setup, uninstall, and intentional user-data retention. From the
    second public release onward, also test an upgrade from the latest published
    release.

There is not yet a single “publishable” build switch. Before publication, run the
build with both `-RequireInstaller` and `-RequireComplianceReady`, complete the
clean-machine lifecycle checks, and either verify Authenticode signatures or
apply the explicit unsigned-release controls documented above.

## Remaining publication risks

- `Automadin.exe` and the installer are not yet code-signed; Windows publisher
  trust remains unresolved. The project is preparing an application to the
  SignPath Foundation open-source signing program.
- Synthetic installer install/upgrade/downgrade/uninstall behavior and an
  empty-runner lifecycle for the exact production installer are automated.
  Interactive first-run, runtime, portability, and clean disposable-VM checks
  still require a manual walkthrough.
- Public hosting must retain the exact hash-pinned corresponding-source set beside
  every binary release; an upstream Qt link alone is intentionally rejected.
- The CycloneDX document covers locked Python distributions while the separately
  verified native inventory covers shipped PE files and Inno Setup. Both
  artifacts are required; neither is a substitute for the other.
