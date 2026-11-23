# Library and backups

## Automation library

The library manages flows, run setups, template images, and visual rules. Group
resources with slash-separated names. Renaming a resource updates supported
internal references; deletion warns about known dependants where applicable.

Individual resources can be imported or exported as JSON or image files. Imports
are preflighted, but exported flows can still contain URLs, text, identifiers, or
other private automation context. Review them before sharing.

## Complete portable backup

**File > Export Portable Backup** creates an `.automadin-bundle` containing a
consistent database snapshot and the ready managed browser profiles you select.
Import verifies paths, limits, checksums, profile identities, and collisions,
then applies the staged workspace on the next application start.

Restoring replaces the complete active database; it is not a merge. Existing
profile directories are not silently overwritten or deleted.

!!! warning "Portable backups can contain active sessions"

    Browser profiles may contain cookies, history, and sign-in tokens. The bundle
    is checksummed but is not encrypted or authenticated. Store and transfer it
    like sensitive account data.

## Database-only transfer

Use **File > Export Database Only** when browser-profile files should be omitted.
The database still contains flow definitions, images, run setups, settings, and
profile records, so it can remain sensitive. Import validates and stages the
database before activating it on restart.
