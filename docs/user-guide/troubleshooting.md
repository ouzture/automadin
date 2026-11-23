# Troubleshooting

## Start with the visible status

1. Reproduce the problem once.
2. Read the preflight message or failed command details.
3. Open **Setup center** for target readiness.
4. Open **Help > Diagnostics** for application and runtime status.
5. Review raw logs only if the privacy-limited report is insufficient.

## Common problems

### Browser is detected but a flow cannot start

Detection proves that files or an executable were found. Use Runtime Manager to
run the browser health check. A specific profile can also be missing, incompatible,
or in use by another run.

### Android device is missing

Confirm ADB is usable, the device is connected, USB debugging is enabled, and the
device-side authorization prompt was accepted. For network ADB, confirm the
device is already listening and reachable.

### Image match is unreliable

Capture a fresh template, crop to a distinctive stable area, reduce the search
region, and test at the actual resolution, scale, and theme. A threshold that is
too low creates false positives; one that is too high rejects harmless rendering
differences.

### OCR returns the wrong value

Tighten the capture region, improve contrast with preprocessing, select the
correct result type and separators, and test several representative screens.

### Scheduled flow did not run

Confirm the scheduler was running, inspect **History**, test the run setup
manually, and verify the target and any browser profile were available at the
scheduled time.

## Sharing a support report

**Help > Diagnostics** previews exactly what **Copy Support Report** copies. It
uses an allowlist and excludes database records, local paths, flow/profile/device
names, URLs, environment variables, and log messages.

Raw logs are separate because they may contain sensitive context. Read them and
redact anything private before posting them in a public issue.
