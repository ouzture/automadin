# Android automation

Android targets use ADB. Install Android Platform Tools through Runtime Manager or
select an existing ADB executable through **Tools > ADB Location**.

## Connect a target

Enable USB debugging on a physical device or start an emulator, then use the
target selector. **Tools > ADB Connect** supports network-connected devices when
the device is already configured to accept that connection. Device authorization
prompts must be accepted on the device itself.

Managed emulator profiles are available through **Tools > Managed Emulators**.
Automadin can acquire a configured managed emulator for a run and release it when
finished.

## Build Android flows

General input commands support taps, swipes, text, and keys. Android lifecycle
commands can install an APK, launch or stop a package, restart an application,
start a specific activity, or open a deep link.

Use the package picker where available instead of manually copying a package
identifier. Application installation changes the target device; test on a device
or emulator intended for automation.

## Android files

The **Android files** page provides read-only browsing and previewing for a
connected device. It is designed for inspection, not remote file management.

## Reliability guidance

- Prefer percentages when a coordinate should scale with the display.
- Use template images or OCR only after testing across the device resolutions you support.
- Keep the device awake and in a known orientation.
- Expect keyboard behavior and permissions to vary by Android version and app.
