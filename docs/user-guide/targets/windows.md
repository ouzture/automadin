# Windows application automation

Windows commands target a configured native application window. **Ensure Windows
App** reacquires the matching window or launches the configured executable when
it is closed.

## Identify the right window

Use stable application and window properties. Titles that include a document,
username, or changing status may need a broader matching rule. Confirm that the
rule does not accidentally select a different window from the same process.

## Input and visual commands

**Win Click**, **Win Drag**, **Win Type**, **Win Key**, **Win Scroll**, and **Win
Focus** send input in the selected window context. Image-relative variants find a
template within that window before clicking or dragging.

Use percentage coordinates for layouts that resize proportionally. Use an image
anchor when the control moves but retains a stable appearance. After any action
that changes screens, wait for observable evidence before continuing.

## Safety

Desktop automation can interact with whichever content the application displays.
Test against non-production data first. Avoid flows that dismiss security prompts,
submit irreversible actions without verification, or type secrets stored directly
in flow parameters.

**Hibernate Computer** ends the current flow and hibernates the local machine.
Keep it in a clearly named, dedicated flow and never place it in an ambiguous
conditional path.
