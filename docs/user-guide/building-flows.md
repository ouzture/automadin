# Build flows

## The editor

The **Flows** page contains a command tree, a searchable **Add command** palette,
and a flow outline. Commands can be moved, nested where supported, collapsed,
disabled, commented, and configured individually.

Save before leaving a changed flow. Version history lets you load an older saved
version into the editor; saving that document creates a new current version.

## Command structure

Simple commands perform one action. Container commands own child commands:

- **For**, **While**, and **While Value** repeat their children.
- **If Image** and **If Value** can run children or an Else branch.
- **Switch Value** owns **Case** branches and a final **Default**.
- **Group** organizes commands without changing execution.

The editor prevents invalid parent/child combinations. Use the outline to jump
through a large flow and enable drag mode only when intentionally reordering.

## Step settings

Each step can have:

- a comment explaining intent;
- a breakpoint for debugging;
- an enabled/disabled state;
- an error policy;
- a retry count where retries are appropriate;
- a minimum wait or pause where supported;
- hook bindings for before/after evidence or recovery behavior.

Use comments to explain *why* a step exists. The command label and parameters
already explain what it does.

## Reuse

Use **Run Flow** to call another saved flow. Inputs pass data into the subflow and
outputs return selected results. Prefer small purpose-focused subflows over
copying a long command sequence into several places.

Use **Section** and **Group** for readability. Folders in a resource name, such as
`Shared/Browser/Open account page`, organize related flows in the library.

## Validate early

Preflight runs before execution and is also used during import. Resolve blockers;
review warnings rather than automatically dismissing them. A document can be
structurally valid while still depending on a target, image, profile, or external
application that is unavailable at runtime.
