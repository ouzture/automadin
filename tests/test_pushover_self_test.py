from __future__ import annotations

from automadin.pushover_self_test import (
    PUSHOVER_SELF_TEST_CLEANUP_FAILED,
    PUSHOVER_SELF_TEST_OK,
    PUSHOVER_SELF_TEST_READ_FAILED,
    run_release_pushover_credential_self_test,
)


class _CredentialApi:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_SESSION = 1

    def __init__(self, *, fail_read: bool = False, fail_delete: bool = False) -> None:
        self.fail_read = fail_read
        self.fail_delete = fail_delete
        self.stored: dict[str, object] | None = None

    def CredWrite(self, value: dict[str, object], flags: int) -> None:  # noqa: N802
        assert flags == 0
        self.stored = dict(value)

    def CredRead(self, target: str, credential_type: int, flags: int):  # noqa: N802
        assert credential_type == self.CRED_TYPE_GENERIC
        assert flags == 0
        if self.fail_read:
            raise RuntimeError("read failed")
        assert self.stored is not None
        assert self.stored["TargetName"] == target
        return dict(self.stored)

    def CredDelete(self, target: str, credential_type: int, flags: int) -> None:  # noqa: N802
        assert credential_type == self.CRED_TYPE_GENERIC
        assert flags == 0
        assert self.stored is not None
        assert self.stored["TargetName"] == target
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.stored = None


def test_release_pushover_self_test_round_trips_and_removes_credential() -> None:
    api = _CredentialApi()

    result = run_release_pushover_credential_self_test(credential_api=api)

    assert result == PUSHOVER_SELF_TEST_OK
    assert api.stored is None


def test_release_pushover_self_test_reports_read_failure_and_still_cleans_up() -> None:
    api = _CredentialApi(fail_read=True)

    result = run_release_pushover_credential_self_test(credential_api=api)

    assert result == PUSHOVER_SELF_TEST_READ_FAILED
    assert api.stored is None


def test_release_pushover_self_test_reports_cleanup_failure() -> None:
    api = _CredentialApi(fail_delete=True)

    result = run_release_pushover_credential_self_test(credential_api=api)

    assert result == PUSHOVER_SELF_TEST_CLEANUP_FAILED
    assert api.stored is not None
