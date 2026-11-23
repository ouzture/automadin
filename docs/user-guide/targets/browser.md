# Browser automation

Automadin uses Playwright for browser sessions. Setup Center can report managed
Chromium, Firefox, and WebKit runtimes and installed Google Chrome or Microsoft
Edge where supported by the current build.

## Choose a session style

An isolated session is useful for public pages and repeatable clean-state tests.
A managed browser profile preserves browser state for workflows that need a
continuing signed-in session. Configure profiles through **Tools > Browser
Profiles** and treat their storage and backups as sensitive.

## Prefer resilient locators

Use **Web Inspector** from the flow editor to inspect a live page and build a
locator. Prefer stable roles, labels, accessible names, or test identifiers over
deep CSS paths and changing generated classes.

Browser element commands use resilient locator data for clicking, filling,
waiting, extraction, and visibility assertions. Verify both the intended match
and the no-match behavior.

## Tabs and popups

Automadin distinguishes tabs it opened from site-created popups. Use durable
selection rules such as URL or title when the live tab order can change. Add
**Ensure Browser Page** after actions that might close the final page.

## Reliability guidance

- Wait for a specific element or navigation state instead of a long fixed delay.
- Assert an observable result after important actions.
- Keep credentials out of flow literals and exported examples.
- Expect websites to change; a locator that worked previously is not permanent.
- Use **Evaluate JS** only when normal element commands cannot express the task;
  page scripts can change application state and are harder to maintain.
