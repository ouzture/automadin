from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from automadin.services.browser_profile_metadata import (
    BrowserProfileIdentity,
    ProfileIdentityField,
    ProfileIdentityObservation,
)
from automadin.services.database import DatabaseService


def _observation(
    *,
    checked_at: str = "2026-07-20T12:00:00+00:00",
    attempted: ProfileIdentityField = ProfileIdentityField.ALL,
    update: ProfileIdentityField = ProfileIdentityField.ALL,
    verified: ProfileIdentityField = ProfileIdentityField.ALL,
    identity: BrowserProfileIdentity | None = None,
) -> ProfileIdentityObservation:
    return ProfileIdentityObservation(
        checked_at=checked_at,
        attempted_mask=attempted,
        update_mask=update,
        verified_mask=verified,
        identity=identity
        or BrowserProfileIdentity(
            browser="Untrusted browser label",
            source_profile="Profile 2",
            account_name="Jane Doe",
            account_email="jane@example.com",
        ),
    )


def test_observation_normalizes_valid_masks_and_is_frozen() -> None:
    observation = ProfileIdentityObservation(
        checked_at="2026-07-20T12:00:00Z",
        attempted_mask=7,
        update_mask=3,
        verified_mask=1,
        identity=BrowserProfileIdentity(),
        detail_code="partial",
    )

    assert observation.attempted_mask is ProfileIdentityField.ALL
    assert observation.update_mask == (
        ProfileIdentityField.SOURCE_PROFILE | ProfileIdentityField.ACCOUNT_NAME
    )
    assert observation.verified_mask is ProfileIdentityField.SOURCE_PROFILE
    with pytest.raises(FrozenInstanceError):
        observation.checked_at = "later"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("attempted", "update", "verified", "message"),
    [
        (8, 0, 0, "invalid bits"),
        (-1, 0, 0, "invalid bits"),
        (1, 2, 0, "subset of attempted"),
        (3, 1, 2, "subset of update"),
    ],
)
def test_observation_rejects_invalid_mask_relationships(
    attempted: int,
    update: int,
    verified: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProfileIdentityObservation(
            checked_at="2026-07-20T12:00:00+00:00",
            attempted_mask=attempted,
            update_mask=update,
            verified_mask=verified,
            identity=BrowserProfileIdentity(),
        )


def test_apply_observation_updates_only_masked_fields_and_freshness(
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    try:
        created = database.create_profile(
            "Work",
            browser="Chrome",
            source_profile="Old source",
            account_name="Old name",
            account_email="old@example.com",
            engine="chromium",
        )
        observation = _observation(
            attempted=ProfileIdentityField.ALL,
            update=(
                ProfileIdentityField.SOURCE_PROFILE
                | ProfileIdentityField.ACCOUNT_EMAIL
            ),
            verified=ProfileIdentityField.SOURCE_PROFILE,
        )

        updated = database.apply_profile_identity_observation(
            created.profile_id,
            observation,
            "chromium",
        )

        assert updated is not None
        assert updated.identity.browser == "Chromium"
        assert updated.identity.source_profile == "Profile 2"
        assert updated.identity.account_name == "Old name"
        assert updated.identity.account_email == "jane@example.com"
        assert updated.engine == "chromium"
        assert updated.identity_checked_at == observation.checked_at
        assert updated.identity_stale_mask == int(
            ProfileIdentityField.ACCOUNT_NAME | ProfileIdentityField.ACCOUNT_EMAIL
        )
    finally:
        database.close()


def test_apply_returns_transaction_snapshot_without_post_commit_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    try:
        created = database.create_profile("Work", engine="chromium")

        def fail_post_commit_read(_profile_id: str):
            raise AssertionError("apply must not re-read after commit")

        monkeypatch.setattr(database, "get_profile", fail_post_commit_read)
        updated = database.apply_profile_identity_observation(
            created.profile_id,
            _observation(),
            "chromium",
        )

        assert updated is not None
        assert updated.identity.account_email == "jane@example.com"
        assert updated.identity_stale_mask == 0
    finally:
        database.close()


def test_apply_observation_accumulates_attempts_and_clears_only_verified_bits(
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    try:
        created = database.create_profile("Work", engine="chromium")
        database._conn.execute(
            "UPDATE profiles SET identity_stale_mask = 0 WHERE profile_id = ?",
            (created.profile_id,),
        )
        database._conn.commit()

        partial = _observation(
            attempted=ProfileIdentityField.ACCOUNT_NAME,
            update=ProfileIdentityField(0),
            verified=ProfileIdentityField(0),
        )
        first = database.apply_profile_identity_observation(
            created.profile_id,
            partial,
            "chromium",
        )
        assert first is not None
        assert first.identity_stale_mask == int(ProfileIdentityField.ACCOUNT_NAME)

        verified = _observation(
            checked_at="2026-07-20T12:01:00+00:00",
            attempted=ProfileIdentityField.ACCOUNT_NAME,
            update=ProfileIdentityField.ACCOUNT_NAME,
            verified=ProfileIdentityField.ACCOUNT_NAME,
        )
        second = database.apply_profile_identity_observation(
            created.profile_id,
            verified,
            "chromium",
        )
        assert second is not None
        assert second.identity_stale_mask == 0
        assert second.identity.account_name == "Jane Doe"
        assert second.identity_checked_at == verified.checked_at
    finally:
        database.close()


def test_optimistic_mismatch_and_missing_profile_make_no_changes(
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    try:
        created = database.create_profile(
            "Work",
            source_profile="Original",
            engine="chromium",
        )
        before = database.get_profile(created.profile_id)

        assert (
            database.apply_profile_identity_observation(
                created.profile_id,
                _observation(),
                "firefox",
            )
            is None
        )
        assert database.get_profile(created.profile_id) == before
        assert (
            database.apply_profile_identity_observation(
                str(uuid.uuid4()),
                _observation(),
                "chromium",
            )
            is None
        )
    finally:
        database.close()


def test_invalid_apply_input_does_not_leave_transaction_open(tmp_path: Path) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    try:
        created = database.create_profile("Work", engine="chromium")
        with pytest.raises(TypeError, match="ProfileIdentityObservation"):
            database.apply_profile_identity_observation(
                created.profile_id,
                object(),
                "chromium",
            )
        assert not database._conn.in_transaction

        with pytest.raises(ValueError, match="one of"):
            database.apply_profile_identity_observation(
                created.profile_id,
                _observation(),
                "safari",
            )
        assert not database._conn.in_transaction
    finally:
        database.close()


@pytest.mark.parametrize(
    "checked_at",
    ["", "2026-07-20T12:00:00", "not-a-timestamp"],
)
def test_observation_rejects_empty_naive_or_malformed_timestamp(
    checked_at: str,
) -> None:
    with pytest.raises(ValueError, match="checked_at"):
        _observation(checked_at=checked_at)


@pytest.mark.parametrize(
    "identity",
    [
        BrowserProfileIdentity(account_name="bad\nname"),
        BrowserProfileIdentity(account_email="x" * 321),
        BrowserProfileIdentity(source_profile=42),  # type: ignore[arg-type]
    ],
)
def test_observation_rejects_unsafe_identity_text(
    identity: BrowserProfileIdentity,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _observation(identity=identity)


def test_observation_rejects_boolean_masks() -> None:
    with pytest.raises(TypeError, match="mask"):
        ProfileIdentityObservation(
            checked_at="2026-07-20T12:00:00Z",
            attempted_mask=True,  # type: ignore[arg-type]
            update_mask=0,
            verified_mask=0,
            identity=BrowserProfileIdentity(),
        )


def test_storage_rebind_resets_freshness_and_metadata_edits_mark_fields_stale(
    tmp_path: Path,
) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    try:
        created = database.create_profile("Work", engine="chromium")
        refreshed = database.apply_profile_identity_observation(
            created.profile_id,
            _observation(),
            "chromium",
        )
        assert refreshed is not None and refreshed.identity_stale_mask == 0

        assert database.update_profile_metadata(
            created.profile_id,
            account_name="Manual name",
        )
        edited = database.get_profile(created.profile_id)
        assert edited is not None
        assert edited.identity_stale_mask == int(ProfileIdentityField.ACCOUNT_NAME)
        assert edited.identity_checked_at == refreshed.identity_checked_at

        assert database.finalize_profile_storage(
            created.profile_id,
            engine="chromium",
        )
        rebound = database.get_profile(created.profile_id)
        assert rebound is not None
        assert rebound.identity_checked_at == ""
        assert rebound.identity_stale_mask == int(ProfileIdentityField.ALL)
    finally:
        database.close()


def test_observation_uses_calling_threads_connection(tmp_path: Path) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    created = database.create_profile("Work", engine="chromium")
    results: list[object] = []

    def apply_from_worker() -> None:
        try:
            results.append(
                database.apply_profile_identity_observation(
                    created.profile_id,
                    _observation(),
                    "chromium",
                )
            )
        finally:
            results.append(database.release_current_thread_connection())

    worker = threading.Thread(target=apply_from_worker)
    worker.start()
    worker.join(timeout=5)
    try:
        assert not worker.is_alive()
        assert results[0] is not None
        assert results[1] is True
        with sqlite3.connect(tmp_path / "profiles.db") as connection:
            checked_at = connection.execute(
                "SELECT identity_checked_at FROM profiles WHERE profile_id = ?",
                (created.profile_id,),
            ).fetchone()[0]
        assert checked_at == "2026-07-20T12:00:00+00:00"
    finally:
        database.close()


def test_compatibility_save_does_not_erase_omitted_metadata(tmp_path: Path) -> None:
    database = DatabaseService(str(tmp_path / "profiles.db"))
    try:
        database.save_profile(
            "Legacy",
            browser="Chrome",
            source_profile="Default",
            account_name="Jane",
            account_email="jane@example.com",
            engine="chromium",
        )
        database.save_profile("Legacy", account_name="Jane Doe")

        profile_id = database.get_profile_id("Legacy")
        assert profile_id is not None
        record = database.get_profile(profile_id)
        assert record is not None
        assert record.identity == BrowserProfileIdentity(
            browser="Chrome",
            source_profile="Default",
            account_name="Jane Doe",
            account_email="jane@example.com",
        )
        assert record.engine == "chromium"
    finally:
        database.close()
