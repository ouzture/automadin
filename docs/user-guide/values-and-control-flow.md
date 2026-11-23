# Values and control flow

Typed values make one flow work with different data while catching incompatible
uses before execution.

## Inputs, variables, outputs, and built-ins

- **Inputs** are supplied by the caller or at launch.
- **Variables** are mutable values owned by the running flow.
- **Outputs** expose final values to a calling flow.
- **Built-ins** provide runtime context made available by Automadin.

Open the flow-value editor from **Flows** to declare a name, type, and default
where applicable. Names should describe meaning, such as `account_name`,
`attempt_count`, or `result_text`.

Commands that support a value reference can use a literal, an Input, a Variable,
or a Built-in. Text templates use double braces, for example
`Welcome {{account_name}}`.

## Branching

Use **If Value** for typed comparisons and **If Image** for visual evidence. Use
**Switch Value** when several mutually exclusive cases depend on one value. Keep
the Default branch as the explicit fallback.

## Repetition

- **For** repeats a fixed number of times.
- **For Each** iterates a list and exposes `{{item}}` and `{{index}}`.
- **For Each Browser Element** snapshots matching elements before iterating them.
- **While Value**, **While**, and **Win While** repeat while their condition holds.

Every condition-based loop should have a clear way to become false and a bounded
failure strategy. Prefer waiting for specific evidence over adding large fixed
delays.

## Flow control

**Terminate Flow** ends at the current point. **Restart Flow** begins again from
the first command. **User Checkpoint** pauses for an intentional manual step. Use
these commands sparingly so termination behavior remains easy to understand.
