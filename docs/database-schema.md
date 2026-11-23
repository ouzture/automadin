# Database schema and migration contract

Automadin stores its schema version in SQLite's `PRAGMA user_version`.

- Current schema version: **8**
- Oldest supported version: **0**
- Supported startup/import range: **0–8**

Version 0 represents databases created before Automadin introduced an explicit
version contract. A compatible version-0 database must contain the original core
tables: `flows`, `images`, `run_configs`, and `profiles`. The 0→1 migration runs in
one SQLite transaction, preserves those records, adds the later tables and profile
metadata columns, validates the complete version-1 shape, and only then records
`user_version = 1`.

The 1→2 migration gives every browser-profile record a canonical UUID in
`profile_id` and records its previous name in `legacy_storage_name`. Saved run
setups that identify exactly one legacy profile are rewritten to the versioned UUID
device format. Ambiguous or malformed references are preserved for diagnosis
instead of being guessed.

Migration does not attach a UUID record to an existing same-name directory. That
would risk connecting an imported database to an unrelated local browser account.
Legacy storage must be adopted explicitly in Browser Profiles; after adoption,
`profiles/<profile UUID>/profile.json` binds the directory to the database identity.
Imported version-2 records whose UUID directories are absent are also shown as
needing setup and can be repaired by importing a copy of the intended browser
profile.

The 2→3 migration adds `identity_checked_at` and a three-bit
`identity_stale_mask` for browser-profile name, signed-in user, and email
freshness. Existing identity labels are preserved, but begin as never checked.
Explicit refresh applies verified values or verified absence field by field in
one transaction; an unavailable or malformed browser file keeps the stored value
and marks only that field stale.

The 3→4 migration adds durable schedules and bounded run history with their
required indexes and foreign-key relationships.

The 4→5 migration gives every current flow and flow-version chain a canonical
UUID. The UUID is stored both in its indexed database column and serialized flow
data, and the two values must match. Existing flows are stamped as legacy value
model version 0; no Inputs, Variables, Outputs, or subflow mappings are inferred.
Flow history is indexed by UUID and save time rather than mutable flow name.
History created before a same-name flow was recreated is retained under a
separate orphan identity and is not shown as history of the replacement flow.
Malformed JSON or inconsistent identities fail the migration transactionally.

The 5→6 migration adds reusable managed Android emulator profiles with stable
profile and target identities.

The 6→7 migration records the source surface size and coordinate space for
stored image templates so adaptive matching can distinguish browser, Android,
and Windows captures.

The 7→8 migration retires Click Moving Target. It recursively removes that
action from current flow trees, nested branches, hook action bodies, and flow
version history. A one-time application setting records affected flow names and
counts for the startup notice. The migration validates every transformed flow
inside the same transaction; malformed data rolls the complete upgrade back.

Databases with a version newer than the running build are rejected before WAL mode
or schema changes are attempted. Users must open those databases with the same or a
newer Automadin release; downgrades are not supported.

## Import safety

The database selected by the user is always opened read-only. Import follows this
sequence:

1. Inspect integrity, identity tables, and schema version on the selected source.
2. Create a SQLite backup in a unique staging file beside the active database.
3. Migrate that staging file, never the selected source.
4. Re-inspect the staged file and require the current schema version.
5. Atomically promote it to the pending-import path.
6. At next startup, validate/migrate the pending copy again before activation.
7. Back up the previous active database. If post-swap validation fails, build and
   validate a sibling restore file, then atomically replace the failed active copy.

A failed migration is rolled back. A failed pending import is moved to a rejected
database file for diagnosis, while the active database remains unchanged. If even
the atomic restore preparation fails (for example, due to a full disk), Automadin
keeps the safety backup and reports its exact path instead of deleting the current
file or silently swallowing the recovery error.

## Adding future migrations

Each release that changes persisted structure must:

1. Increase `CURRENT_SCHEMA_VERSION` by one.
2. Add the matching `DatabaseService._migrate_vN_to_vN+1` method.
3. Keep the migration compatible with a single SQLite transaction; do not use APIs
   that implicitly commit midway through the migration.
4. Update the current table/column contract and add upgrade, rollback, import, and
   future-version rejection tests.

The migration contract covers the SQLite database only. Browser profile folders
remain separate user data and are not included in database import/export.
`profile.json` contains only the stable profile ID, browser engine, manifest format
and version, and creation timestamp; authentication data remains in the browser
profile directory. Moving a database to another PC therefore requires explicit
profile reconciliation and may require signing in again.
