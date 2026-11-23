"""Packaged release gate for Windows Credential Manager integration."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4


PUSHOVER_SELF_TEST_OK = 0
PUSHOVER_SELF_TEST_UNAVAILABLE = 76
PUSHOVER_SELF_TEST_WRITE_FAILED = 77
PUSHOVER_SELF_TEST_READ_FAILED = 78
PUSHOVER_SELF_TEST_CLEANUP_FAILED = 79

_SELF_TEST_TARGET_PREFIX = "Automadin/ReleaseSelfTest/"
_SELF_TEST_PAYLOAD = "automadin-release-self-test"


def run_release_pushover_credential_self_test(
    *,
    credential_api: Any | None = None,
) -> int:
    """Round-trip a disposable generic credential through the packaged runtime."""
    if credential_api is None:
        try:
            # CredRead uses win32timezone lazily while converting LastWritten.
            import win32timezone  # type: ignore  # noqa: F401
            import win32cred  # type: ignore
        except ImportError:
            return PUSHOVER_SELF_TEST_UNAVAILABLE
        credential_api = win32cred

    target = f"{_SELF_TEST_TARGET_PREFIX}{uuid4().hex}"
    created = False
    outcome = PUSHOVER_SELF_TEST_WRITE_FAILED
    try:
        credential_api.CredWrite(
            {
                "Type": credential_api.CRED_TYPE_GENERIC,
                "TargetName": target,
                "CredentialBlob": _SELF_TEST_PAYLOAD,
                "Persist": credential_api.CRED_PERSIST_SESSION,
                "UserName": "Automadin Release Self-Test",
                "Comment": "Disposable Automadin release verification credential",
            },
            0,
        )
        created = True
        outcome = PUSHOVER_SELF_TEST_READ_FAILED
        stored = credential_api.CredRead(
            target,
            credential_api.CRED_TYPE_GENERIC,
            0,
        )
        if isinstance(stored, Mapping) and stored.get("TargetName") == target:
            outcome = PUSHOVER_SELF_TEST_OK
    except Exception:
        pass
    finally:
        if created:
            try:
                credential_api.CredDelete(
                    target,
                    credential_api.CRED_TYPE_GENERIC,
                    0,
                )
            except Exception:
                outcome = PUSHOVER_SELF_TEST_CLEANUP_FAILED
    return outcome
