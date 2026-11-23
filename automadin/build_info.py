"""Qt-free application identity helpers for source and packaged builds."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version as metadata_version
from typing import Any

from automadin.runtime import application_dir

RELEASE_MANIFEST_NAME = "release-manifest.json"
RELEASE_MANIFEST_SCHEMA_VERSION = 1


def _manifest_version(payload: Any) -> str | None:
    """Return a trusted application version from one decoded release manifest."""
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != RELEASE_MANIFEST_SCHEMA_VERSION:
        return None

    application = payload.get("application")
    if not isinstance(application, dict) or application.get("name") != "Automadin":
        return None

    value = application.get("version")
    if not isinstance(value, str):
        return None
    version = value.strip()
    if not version or version != value or len(version) > 100:
        return None
    if any(character.isspace() or ord(character) < 32 for character in version):
        return None
    return version


def packaged_app_version(development_label: str) -> str:
    """Return release identity, installed metadata, or ``development_label``.

    A frozen distribution is not necessarily installed as a Python package, so
    its adjacent release manifest is the authoritative identity. Malformed or
    incomplete manifests are ignored rather than allowing packaging metadata to
    break startup and diagnostic screens.
    """
    manifest_path = application_dir() / RELEASE_MANIFEST_NAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    else:
        manifest_version = _manifest_version(payload)
        if manifest_version is not None:
            return manifest_version

    try:
        installed_version = metadata_version("automadin").strip()
    except (PackageNotFoundError, OSError, ValueError):
        return development_label
    return installed_version or development_label
