# Database schema and data safety

Automadin stores application state in SQLite. The current schema version is
recorded in SQLite's `PRAGMA user_version`; the value in
`automadin.services.database.CURRENT_SCHEMA_VERSION` is the source of truth.

## Current data model

The database contains these application-owned tables:

| Table | Purpose |
| --- | --- |
| `flows` | Current flow definitions and stable flow identities |
| `flow_versions` | Saved flow history |
| `images` | Template images and their source-surface metadata |
| `run_configs` | Reusable run setups |
| `schedules` | Durable schedule definitions |
| `run_history` | Bounded execution history |
| `profiles` | Browser-profile records and identity status |
| `managed_emulator_profiles` | Reusable managed Android emulator definitions |
| `visual_rules` | Reusable visual-recognition rules |
| `app_settings` | Application settings and state |

`CURRENT_SCHEMA_TABLES` and `CURRENT_SCHEMA_COLUMNS` in
`automadin/services/database.py` define the exact required shape. Startup validates
that shape before normal database use. Stable UUIDs identify flows, browser
profiles, and managed emulator profiles independently of their display names.

Browser profile folders are separate user data and are not stored in SQLite.
Their `profile.json` files contain only the stable profile ID, browser engine,
manifest format and version, and creation timestamp. Authentication data remains
inside the browser profile directory.

## Import and restore safety

Automadin never modifies a database selected by the user in place. Database-only
import follows this sequence:

1. Open the selected source read-only and inspect its integrity and schema.
2. Copy it into a unique staging file beside the active database.
3. Validate the staged database against the current application contract.
4. Atomically promote the validated file to the pending-import path.
5. Validate the pending copy again at startup before activation.
6. Back up the active database before replacement. If post-swap validation fails,
   restore it through a separately validated sibling file.

A rejected pending import is retained separately for diagnosis while the active
database remains unchanged. If restore preparation fails, Automadin keeps the
safety backup and reports its path instead of deleting the active file or hiding
the recovery error.

Portable backups may also include selected browser profile folders. Because those
folders can contain cookies and sign-in tokens, portable backups must be handled as
sensitive files. Moving data to another computer can still require profile
reconciliation and signing in again.

## Changing persisted data

After the first public release, a schema change must preserve data created by the
latest published release. For each change:

1. Increase `CURRENT_SCHEMA_VERSION` by one.
2. Add one transactional schema transition in `DatabaseService`.
3. Avoid SQLite operations that implicitly commit midway through the transition.
4. Update the current table and column contracts.
5. Add tests for current-schema creation, transition success, rollback, import,
   and rejection of databases newer than the running application.

Do not use display names as durable foreign identities, infer links to local
browser-profile folders, or partially activate unvalidated imported data.
