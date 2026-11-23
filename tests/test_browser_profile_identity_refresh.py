from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from automadin.services.browser_profile_identity_refresh import (
    IdentityRefreshStatus,
    _ReadState,
    observe_browser_profile_identity,
    refresh_browser_profile_identity,
)
from automadin.services.browser_profile_leases import (
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.browser_profile_manifest import (
    _write_profile_manifest as write_profile_manifest,
    BrowserProfileManifest,
)
from automadin.services.browser_profile_metadata import (
    BrowserProfileIdentity,
    ProfileIdentityField,
    StoredBrowserProfile,
)


PROFILE_ID = "12345678-1234-4abc-9def-1234567890ab"
CHECKED_AT = "2026-07-20T12:00:00Z"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _chromium_root(tmp_path: Path) -> Path:
    root = tmp_path / PROFILE_ID
    (root / "Default").mkdir(parents=True)
    return root


def test_chromium_uses_exact_default_cache_and_unique_primary_account(
    tmp_path: Path,
) -> None:
    root = _chromium_root(tmp_path)
    _write_json(
        root / "Local State",
        {
            "profile": {
                "info_cache": {
                    "Profile 1": {"name": "Wrong"},
                    "Default": {"name": " Work profile "},
                }
            }
        },
    )
    _write_json(
        root / "Default" / "Preferences",
        {
            "profile": {"name": " Work profile "},
            "account_info": [
                {"full_name": "Other", "email": "other@example.test"},
                {
                    "full_name": " Person ",
                    "email": "person@example.test",
                    "is_primary_account": True,
                },
            ]
        },
    )

    result = observe_browser_profile_identity(
        root,
        "chromium",
        checked_at=CHECKED_AT,
    )

    assert result.update_mask == ProfileIdentityField.ALL
    assert result.verified_mask == ProfileIdentityField.ALL
    assert result.identity.source_profile == "Work profile"
    assert result.identity.account_name == "Person"
    assert result.identity.account_email == "person@example.test"


def test_chromium_never_chooses_arbitrary_first_account(tmp_path: Path) -> None:
    root = _chromium_root(tmp_path)
    _write_json(
        root / "Local State",
        {"profile": {"info_cache": {"Default": {"name": "Default"}}}},
    )
    _write_json(
        root / "Default" / "Preferences",
        {
            "profile": {"name": "Default"},
            "account_info": [
                {"full_name": "First", "email": "first@example.test"},
                {"full_name": "Second", "email": "second@example.test"},
            ]
        },
    )

    result = observe_browser_profile_identity(root, "chrome")

    assert result.update_mask == ProfileIdentityField.SOURCE_PROFILE
    assert result.identity.account_name == ""
    assert result.identity.account_email == ""
    assert "ambiguous" in result.detail_code


def test_invalid_explicit_primary_does_not_fall_back_to_valid_secondary(
    tmp_path: Path,
) -> None:
    root = _chromium_root(tmp_path)
    _write_json(
        root / "Default" / "Preferences",
        {
            "profile": {"name": "Default"},
            "account_info": [
                {
                    "full_name": 42,
                    "email": "",
                    "is_primary_account": True,
                },
                {
                    "full_name": "Secondary",
                    "email": "secondary@example.test",
                },
            ],
        },
    )

    result = observe_browser_profile_identity(root, "chromium")

    assert result.update_mask == ProfileIdentityField.SOURCE_PROFILE
    assert result.identity.account_name == ""
    assert result.identity.account_email == ""
    assert "primary_account_invalid" in result.detail_code


def test_account_name_and_email_are_updated_as_one_consistent_unit(
    tmp_path: Path,
) -> None:
    root = _chromium_root(tmp_path)
    _write_json(
        root / "Local State",
        {"profile": {"info_cache": {"Default": {"name": "Default"}}}},
    )
    _write_json(
        root / "Default" / "Preferences",
        {
            "profile": {"name": "Default"},
            "account_info": [{"full_name": 42, "email": "valid@example.test"}],
        },
    )

    result = observe_browser_profile_identity(root, "msedge")

    # The pair is cleared together for consistency, while only the field with
    # valid direct evidence is marked fresh.
    assert result.update_mask & ProfileIdentityField.ACCOUNT_NAME
    assert result.update_mask & ProfileIdentityField.ACCOUNT_EMAIL
    assert not result.verified_mask & ProfileIdentityField.ACCOUNT_NAME
    assert result.verified_mask & ProfileIdentityField.ACCOUNT_EMAIL


@pytest.mark.parametrize(
    ("payload_size", "expected_code"),
    [
        (0, "invalid"),
        (4 * 1024 * 1024 + 1, "oversized"),
    ],
    ids=("malformed", "oversized"),
)
def test_malformed_and_oversized_preferences_are_unknown_not_empty(
    tmp_path: Path,
    payload_size: int,
    expected_code: str,
) -> None:
    root = _chromium_root(tmp_path)
    _write_json(
        root / "Local State",
        {"profile": {"info_cache": {"Default": {"name": "Default"}}}},
    )
    payload = "x" * payload_size if payload_size else "{broken"
    (root / "Default" / "Preferences").write_text(payload, encoding="utf-8")

    result = observe_browser_profile_identity(root, "chromium")

    assert result.update_mask == ProfileIdentityField(0)
    assert expected_code in result.detail_code


def test_symlinked_managed_file_is_rejected(tmp_path: Path) -> None:
    root = _chromium_root(tmp_path)
    outside = tmp_path / "outside.json"
    _write_json(outside, {"account_info": []})
    try:
        (root / "Default" / "Preferences").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform.")

    result = observe_browser_profile_identity(root, "chromium")

    assert not result.update_mask & ProfileIdentityField.ACCOUNT_NAME
    assert "unsafe" in result.detail_code


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point open hardening")
def test_windows_open_race_rejects_swapped_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automadin.services import browser_profile_identity_refresh as service

    root = _chromium_root(tmp_path)
    preferences = root / "Default" / "Preferences"
    outside = tmp_path / "outside.json"
    _write_json(preferences, {"profile": {"name": "Default"}, "account_info": []})
    _write_json(
        outside,
        {
            "profile": {"name": "Redirected"},
            "account_info": [{"email": "outside@example.test"}],
        },
    )
    real_open = service._open_readonly_no_follow

    def swap_then_open(path: Path, managed_root: Path) -> int:
        if path == preferences and not path.is_symlink():
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError:
                pytest.skip("Symlink creation is unavailable on this platform.")
        return real_open(path, managed_root)

    monkeypatch.setattr(service, "_open_readonly_no_follow", swap_then_open)
    result = observe_browser_profile_identity(root, "chromium")

    assert not result.verified_mask & ProfileIdentityField.ACCOUNT_EMAIL
    assert result.identity.account_email == ""
    assert "unsafe" in result.detail_code


@pytest.mark.skipif(os.name != "nt", reason="Windows junction containment hardening")
def test_windows_open_race_rejects_swapped_intermediate_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automadin.services import browser_profile_identity_refresh as service

    root = _chromium_root(tmp_path)
    preferences = root / "Default" / "Preferences"
    outside = tmp_path / "outside-default"
    outside.mkdir()
    _write_json(preferences, {"profile": {"name": "Default"}, "account_info": []})
    _write_json(
        outside / "Preferences",
        {
            "profile": {"name": "Redirected"},
            "account_info": [{"email": "outside@example.test"}],
        },
    )
    real_open = service._open_readonly_no_follow

    def swap_then_open(path: Path, managed_root: Path) -> int:
        if path == preferences and not path.parent.is_symlink():
            path.unlink()
            path.parent.rmdir()
            try:
                path.parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("Directory symlink creation is unavailable.")
        return real_open(path, managed_root)

    monkeypatch.setattr(service, "_open_readonly_no_follow", swap_then_open)
    result = observe_browser_profile_identity(root, "chromium")

    assert not result.verified_mask & ProfileIdentityField.ACCOUNT_EMAIL
    assert result.identity.account_email == ""
    assert "unsafe" in result.detail_code


def test_a_file_that_changes_twice_remains_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from automadin.services import browser_profile_identity_refresh as service

    root = _chromium_root(tmp_path)
    original = service._read_once

    def changing(path: Path, parts: tuple[str, ...], limit: int):
        if parts == ("Default", "Preferences"):
            return _ReadState.CHANGED, b""
        return original(path, parts, limit)

    monkeypatch.setattr(service, "_read_once", changing)

    result = observe_browser_profile_identity(root, "chromium")

    assert result.update_mask == ProfileIdentityField(0)
    assert "changed" in result.detail_code


def test_firefox_source_is_unattempted_and_stable_absence_means_signed_out(
    tmp_path: Path,
) -> None:
    root = tmp_path / "firefox"
    root.mkdir()

    result = observe_browser_profile_identity(
        root,
        "firefox",
        checked_at=CHECKED_AT,
    )

    assert not result.attempted_mask & ProfileIdentityField.SOURCE_PROFILE
    assert result.update_mask == (
        ProfileIdentityField.ACCOUNT_NAME | ProfileIdentityField.ACCOUNT_EMAIL
    )
    assert result.identity.account_name == ""
    assert result.identity.account_email == ""
    assert result.detail_code == "firefox_signed_out"


def test_firefox_valid_signed_in_user_is_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "firefox"
    root.mkdir()
    _write_json(
        root / "signedInUser.json",
        {
            "accountData": {
                "displayName": "  Ada   Lovelace ",
                "email": "ada@example.test",
            }
        },
    )

    result = observe_browser_profile_identity(root, "firefox")

    assert result.identity.account_name == "Ada Lovelace"
    assert result.identity.account_email == "ada@example.test"
    assert result.update_mask == result.verified_mask


class _FakeDatabase:
    def __init__(self, record: StoredBrowserProfile | None) -> None:
        self.record = record
        self.released = 0
        self.applies = 0

    def get_profile(self, profile_id: str) -> StoredBrowserProfile | None:
        return self.record if self.record and self.record.profile_id == profile_id else None

    def apply_profile_identity_observation(
        self,
        profile_id: str,
        observation,
        expected_engine: str,
    ) -> StoredBrowserProfile | None:
        self.applies += 1
        if self.record is None or self.record.engine != expected_engine:
            return None
        old = self.record.identity
        new = BrowserProfileIdentity(
            browser=old.browser,
            source_profile=(
                observation.identity.source_profile
                if observation.update_mask & ProfileIdentityField.SOURCE_PROFILE
                else old.source_profile
            ),
            account_name=(
                observation.identity.account_name
                if observation.update_mask & ProfileIdentityField.ACCOUNT_NAME
                else old.account_name
            ),
            account_email=(
                observation.identity.account_email
                if observation.update_mask & ProfileIdentityField.ACCOUNT_EMAIL
                else old.account_email
            ),
        )
        stale = (
            int(self.record.identity_stale_mask)
            | int(observation.attempted_mask)
        ) & ~int(observation.verified_mask)
        self.record = replace(
            self.record,
            identity=new,
            identity_checked_at=observation.checked_at,
            identity_stale_mask=stale,
        )
        return self.record

    def release_current_thread_connection(self) -> bool:
        self.released += 1
        return True


def _ready_profile(tmp_path: Path) -> tuple[Path, StoredBrowserProfile]:
    profiles = tmp_path / "profiles"
    root = profiles / PROFILE_ID
    (root / "Default").mkdir(parents=True)
    write_profile_manifest(
        root,
        BrowserProfileManifest.create("chromium", profile_id=PROFILE_ID),
    )
    _write_json(
        root / "Local State",
        {"profile": {"info_cache": {"Default": {"name": "Default"}}}},
    )
    _write_json(
        root / "Default" / "Preferences",
        {"profile": {"name": "Default"}, "account_info": []},
    )
    record = StoredBrowserProfile(
        profile_id=PROFILE_ID,
        name="Profile",
        created_at=CHECKED_AT,
        identity=BrowserProfileIdentity(
            source_profile="Old",
            account_name="Old",
            account_email="old@example.test",
        ),
        engine="chromium",
    )
    return profiles, record


def test_coordinator_applies_refresh_and_releases_lease_and_connection(
    tmp_path: Path,
) -> None:
    profiles, record = _ready_profile(tmp_path)
    database = _FakeDatabase(record)
    registry = BrowserProfileLeaseRegistry()

    result = refresh_browser_profile_identity(
        PROFILE_ID,
        database,
        lease_registry=registry,
        profiles_root=profiles,
    )

    assert result.status is IdentityRefreshStatus.UPDATED
    assert result.changed_fields == (
        "source_profile",
        "account_name",
        "account_email",
    )
    assert not result.stale_fields
    assert database.released == 1
    assert registry.snapshot(PROFILE_ID) is None
    assert "old@example.test" not in result.message


def test_committed_refresh_reports_cleanup_uncertainty_truthfully(
    tmp_path: Path,
) -> None:
    profiles, record = _ready_profile(tmp_path)
    database = _FakeDatabase(record)

    class _Lease:
        def release(self) -> bool:
            return False

    class _Registry:
        def acquire_maintenance(self, _profile_id, _use):
            return _Lease()

    result = refresh_browser_profile_identity(
        PROFILE_ID,
        database,
        lease_registry=_Registry(),  # type: ignore[arg-type]
        profiles_root=profiles,
    )

    assert database.applies == 1
    assert result.status is IdentityRefreshStatus.CLEANUP_FAILED
    assert "was saved" in result.message
    assert "Restart Automadin" in result.message


def test_committed_refresh_reports_database_cleanup_uncertainty(
    tmp_path: Path,
) -> None:
    profiles, record = _ready_profile(tmp_path)

    class _CleanupFailDatabase(_FakeDatabase):
        def release_current_thread_connection(self) -> bool:
            self.released += 1
            return False

    database = _CleanupFailDatabase(record)
    result = refresh_browser_profile_identity(
        PROFILE_ID,
        database,
        lease_registry=BrowserProfileLeaseRegistry(),
        profiles_root=profiles,
    )

    assert database.applies == 1
    assert result.status is IdentityRefreshStatus.CLEANUP_FAILED
    assert "was saved" in result.message


def test_active_owner_conflict_is_actionable_and_cleanup_still_runs(
    tmp_path: Path,
) -> None:
    profiles, record = _ready_profile(tmp_path)
    database = _FakeDatabase(record)
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(
        PROFILE_ID,
        ProfileUse("run", "Active browser", "run-1"),
    )

    result = refresh_browser_profile_identity(
        PROFILE_ID,
        database,
        lease_registry=registry,
        profiles_root=profiles,
    )

    assert result.status is IdentityRefreshStatus.BUSY
    assert "Close" in result.message
    assert database.released == 1
    assert registry.snapshot(PROFILE_ID) is not None
    owner.release()


def test_cancellation_before_filesystem_work_releases_without_applying(
    tmp_path: Path,
) -> None:
    profiles, record = _ready_profile(tmp_path)
    database = _FakeDatabase(record)
    registry = BrowserProfileLeaseRegistry()

    result = refresh_browser_profile_identity(
        PROFILE_ID,
        database,
        lease_registry=registry,
        profiles_root=profiles,
        interrupted=lambda: True,
    )

    assert result.status is IdentityRefreshStatus.CANCELLED
    assert database.applies == 0
    assert database.released == 1
    assert registry.snapshot(PROFILE_ID) is None


def test_cancellation_immediately_before_commit_preserves_database(
    tmp_path: Path,
) -> None:
    profiles, record = _ready_profile(tmp_path)
    database = _FakeDatabase(record)
    calls = 0

    def interrupted() -> bool:
        nonlocal calls
        calls += 1
        return calls == 3

    result = refresh_browser_profile_identity(
        PROFILE_ID,
        database,
        lease_registry=BrowserProfileLeaseRegistry(),
        profiles_root=profiles,
        interrupted=interrupted,
    )

    assert result.status is IdentityRefreshStatus.CANCELLED
    assert calls == 3
    assert database.applies == 0
    assert database.record == record
