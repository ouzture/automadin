# Run and debug

## Preflight

Before execution, Automadin checks the flow tree, referenced resources and
subflows, value bindings, target compatibility, and relevant runtime readiness.
Blockers prevent a run; warnings require review because the condition may be
intentional or runtime-dependent.

## Flow debugger

Use **Flow debugger** while developing a flow. It provides live status, execution
logs, pause/continue, stop, and run-again controls. Breakpoints pause before the
marked step so you can inspect the target and values.

Use **Run step** in the editor for a focused command test. A single step still
needs a valid target and context; testing it does not prove that the complete flow
reaches the same state.

## Errors and retries

Open the failure details to identify the command, phase, and underlying message.
Use retries only for genuinely transient behavior. A missing locator, wrong
package, or invalid input does not become correct through repeated execution.

Design important steps as action plus evidence:

1. perform the action;
2. wait for the expected state;
3. assert or branch on that state;
4. stop with a useful error when it does not appear.

## Active runs

The **Runs** page shows active runs and provides a compact mini player. Multiple
runs may compete for a target or browser profile; Automadin uses leases where
required and reports when a resource is already in use.
