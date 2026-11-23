# Privacy and safe sharing

Automadin runs locally, but the data needed to automate a task can still be
sensitive. A public issue, exported flow, screenshot, or backup can reveal more
than expected.

## Never publish these without careful review

- Automadin databases and `.automadin-bundle` backups.
- Managed browser profiles, cookies, tokens, or browser history.
- Notification credentials, API tokens, passwords, or one-time codes.
- Raw logs, local filesystem paths, account names, device identifiers, or private URLs.
- Screenshots or template images containing personal, customer, or production data.
- Exported flows that embed credentials or private form content.

## Safer examples and bug reports

Use synthetic names, public websites, and placeholder identifiers. Reproduce an
issue in a fresh workspace when possible. Use **Help > Diagnostics** first because
its copied report intentionally excludes records, names, paths, URLs, and log
messages.

## Flow safety

- Test new flows against disposable or non-production data.
- Add explicit evidence after important actions.
- Keep destructive or system-power steps in clearly named paths.
- Use checkpoints when human review is required.
- Do not build flows intended to bypass security controls or authorization.
- Review imported flows before running them; a flow is executable automation, not
  a passive document.

Report suspected vulnerabilities privately using the instructions in the
[security policy](https://github.com/ouzture/automadin/blob/main/SECURITY.md).
