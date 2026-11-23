from __future__ import annotations

import threading
import uuid

import pytest

from automadin.services.browser_profile_leases import (
    BrowserProfileBorrowActiveError,
    BrowserProfileInUseError,
    BrowserProfileLeaseStateError,
    BrowserProfileLeaseRegistry,
    MaintenanceProfileLease,
    ProfileUse,
)
from automadin.services.browser_profile_paths import InvalidBrowserProfileIdError


PROFILE_ID = "f234e28f-93ee-4ef4-a81d-1b6e187ff344"
OTHER_PROFILE_ID = "02537594-806b-4856-a019-a5c2512e108c"
THIRD_PROFILE_ID = "7887ff9d-8fb1-42ff-8915-e8b1715935df"


def _use(kind: str, operation_id: str = "operation-1") -> ProfileUse:
    return ProfileUse(kind=kind, label=f"{kind.title()} session", operation_id=operation_id)


def test_profile_ids_are_canonicalized_at_the_registry_boundary() -> None:
    registry = BrowserProfileLeaseRegistry()

    with pytest.raises(InvalidBrowserProfileIdError):
        registry.acquire_owner(PROFILE_ID.upper(), _use("run"))

    lease = registry.acquire_owner(uuid.UUID(PROFILE_ID), _use("run"))
    assert lease.profile_id == PROFILE_ID
    lease.release()


def test_same_profile_has_one_owner_across_all_owner_kinds() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))

    with pytest.raises(BrowserProfileInUseError) as caught:
        registry.acquire_owner(PROFILE_ID, _use("design", "operation-2"))

    assert caught.value.current.mode == "owner"
    assert caught.value.current.use == owner.use
    assert caught.value.requested.mode == "owner"
    assert caught.value.requested.use.kind == "design"
    assert PROFILE_ID in str(caught.value)
    assert "Run session" in str(caught.value)


def test_different_profiles_do_not_block_each_other() -> None:
    registry = BrowserProfileLeaseRegistry()
    first = registry.acquire_owner(PROFILE_ID, _use("run"))
    second = registry.acquire_maintenance(OTHER_PROFILE_ID, _use("delete"))

    assert first.profile_id != second.profile_id
    first.release()
    second.release()


@pytest.mark.parametrize("first", ["owner", "maintenance"])
@pytest.mark.parametrize("second", ["owner", "maintenance"])
def test_primary_lease_exclusion_matrix(first: str, second: str) -> None:
    registry = BrowserProfileLeaseRegistry()
    acquire_first = (
        registry.acquire_owner if first == "owner" else registry.acquire_maintenance
    )
    acquire_second = (
        registry.acquire_owner if second == "owner" else registry.acquire_maintenance
    )
    held = acquire_first(PROFILE_ID, _use(first))

    with pytest.raises(BrowserProfileInUseError) as caught:
        acquire_second(PROFILE_ID, _use(second, "operation-2"))

    assert caught.value.current.mode == first
    assert caught.value.requested.mode == second
    held.release()


def test_release_is_idempotent_and_a_stale_generation_cannot_release_new_owner() -> None:
    registry = BrowserProfileLeaseRegistry()
    old_owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    old_generation = old_owner.generation
    assert old_owner.release() is True
    assert old_owner.release() is False

    new_owner = registry.acquire_owner(PROFILE_ID, _use("design", "operation-2"))
    assert new_owner.generation > old_generation

    # Exercise the token/generation guard directly: an obsolete handle cannot
    # clear a newer entry even if release is attempted again.
    assert registry._release_primary(  # noqa: SLF001
        PROFILE_ID,
        old_generation,
        old_owner._token,  # noqa: SLF001
        "owner",
    ) is False
    assert registry.snapshot(PROFILE_ID).generation == new_owner.generation
    new_owner.release()


def test_owner_can_be_released_from_another_thread() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    result: list[bool] = []

    thread = threading.Thread(target=lambda: result.append(owner.release()))
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result == [True]
    replacement = registry.acquire_owner(PROFILE_ID, _use("design"))
    replacement.release()


def test_borrow_can_be_released_from_another_thread() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    borrow = owner.borrow(_use("design"))
    result: list[bool] = []

    thread = threading.Thread(target=lambda: result.append(borrow.release()))
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result == [True]
    owner.release()


def test_borrow_is_exclusive_and_tied_to_the_exact_live_owner_generation() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    borrow = owner.borrow(_use("design", "operation-2"))

    with pytest.raises(BrowserProfileInUseError) as caught:
        owner.borrow(_use("preview", "operation-3"))
    assert caught.value.current.borrowed_by == borrow.use
    assert caught.value.requested.owner_generation == owner.generation

    with pytest.raises(BrowserProfileBorrowActiveError) as close_error:
        owner.release()
    assert close_error.value.current.borrowed_by == borrow.use
    assert owner.released is False

    assert borrow.release() is True
    assert borrow.release() is False
    assert owner.release() is True

    replacement = registry.acquire_owner(PROFILE_ID, _use("run", "operation-4"))
    with pytest.raises(BrowserProfileInUseError):
        registry.acquire_borrow(owner, _use("design", "operation-5"))
    replacement.release()


def test_maintenance_excludes_borrow_and_owner() -> None:
    registry = BrowserProfileLeaseRegistry()
    maintenance = registry.acquire_maintenance(PROFILE_ID, _use("import"))

    with pytest.raises(BrowserProfileInUseError):
        registry.acquire_owner(PROFILE_ID, _use("run"))

    maintenance.release()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    borrow = owner.borrow(_use("design"))
    with pytest.raises(BrowserProfileInUseError):
        registry.acquire_maintenance(PROFILE_ID, _use("delete"))
    borrow.release()
    owner.release()


def test_context_managers_release_their_leases() -> None:
    registry = BrowserProfileLeaseRegistry()
    with registry.acquire_owner(PROFILE_ID, _use("run")) as owner:
        with owner.borrow(_use("design")):
            assert registry.snapshot(PROFILE_ID).borrowed_by is not None

    assert registry.snapshot(PROFILE_ID) is None


def test_owner_validation_rejects_profile_mismatch_and_released_or_stale_handles() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    owner.ensure_live(PROFILE_ID)
    with pytest.raises(BrowserProfileLeaseStateError, match="different profile"):
        owner.ensure_live(OTHER_PROFILE_ID)

    owner.release()
    with pytest.raises(BrowserProfileLeaseStateError, match="released"):
        owner.ensure_live()


def test_begin_close_atomically_blocks_new_borrows_until_cancel_or_release() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    assert owner.begin_close() is True
    assert owner.begin_close() is False
    assert registry.snapshot(PROFILE_ID).closing is True

    with pytest.raises(BrowserProfileInUseError):
        owner.borrow(_use("design"))
    with pytest.raises(BrowserProfileLeaseStateError, match="closing"):
        owner.ensure_live()

    assert owner.cancel_close() is True
    with owner.borrow(_use("design")):
        pass
    owner.begin_close()
    assert owner.release() is True


def test_begin_close_rejects_an_active_borrow_without_entering_closing_state() -> None:
    registry = BrowserProfileLeaseRegistry()
    owner = registry.acquire_owner(PROFILE_ID, _use("run"))
    borrow = owner.borrow(_use("design"))

    with pytest.raises(BrowserProfileBorrowActiveError):
        owner.begin_close()
    assert registry.snapshot(PROFILE_ID).closing is False

    borrow.release()
    owner.release()


def test_maintenance_validation_rejects_mismatch_released_and_stale_handles() -> None:
    registry = BrowserProfileLeaseRegistry()
    lease = registry.acquire_maintenance(PROFILE_ID, _use("repair"))

    lease.ensure_live(PROFILE_ID)
    with pytest.raises(BrowserProfileLeaseStateError, match="different profile"):
        lease.ensure_live(OTHER_PROFILE_ID)

    assert registry._release_primary(  # noqa: SLF001
        PROFILE_ID,
        lease.generation,
        lease._token,  # noqa: SLF001
        "maintenance",
    )
    with pytest.raises(BrowserProfileLeaseStateError, match="superseded"):
        lease.ensure_live()

    replacement = registry.acquire_maintenance(PROFILE_ID, _use("repair", "operation-2"))
    replacement.release()
    assert lease.release() is False
    with pytest.raises(BrowserProfileLeaseStateError, match="released"):
        lease.ensure_live()


def test_many_maintenance_acquisition_is_sorted_deduplicated_and_registry_backed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BrowserProfileLeaseRegistry()
    acquired: list[str] = []
    original = registry._acquire_primary  # noqa: SLF001

    def track(profile_id: str, mode: str, use: ProfileUse):
        acquired.append(profile_id)
        return original(profile_id, mode, use)  # type: ignore[arg-type]

    monkeypatch.setattr(registry, "_acquire_primary", track)
    scope = registry.acquire_maintenance_many(
        [THIRD_PROFILE_ID, PROFILE_ID, OTHER_PROFILE_ID, PROFILE_ID],
        _use("portable-restore"),
    )

    expected = tuple(sorted({PROFILE_ID, OTHER_PROFILE_ID, THIRD_PROFILE_ID}))
    assert scope.profile_ids == expected
    assert acquired == list(expected)
    for profile_id in expected:
        lease = scope.for_profile(profile_id)
        lease.ensure_live(profile_id)
        assert lease._registry is registry  # noqa: SLF001

    with pytest.raises(BrowserProfileLeaseStateError, match="outside"):
        scope.for_profile(str(uuid.uuid4()))
    assert scope.release() is True
    assert scope.release() is False
    with pytest.raises(BrowserProfileLeaseStateError, match="scope was released"):
        scope.for_profile(PROFILE_ID)


def test_many_maintenance_conflict_unwinds_partial_acquisition_in_reverse() -> None:
    registry = BrowserProfileLeaseRegistry()
    held = registry.acquire_owner(PROFILE_ID, _use("run"))

    with pytest.raises(BrowserProfileInUseError):
        registry.acquire_maintenance_many(
            [PROFILE_ID, OTHER_PROFILE_ID],
            _use("portable-restore"),
        )

    assert registry.snapshot(OTHER_PROFILE_ID) is None
    assert registry.snapshot(PROFILE_ID).use == held.use
    replacement = registry.acquire_maintenance(OTHER_PROFILE_ID, _use("repair"))
    replacement.release()
    held.release()


def test_many_maintenance_canonicalizes_all_ids_before_acquiring_any() -> None:
    registry = BrowserProfileLeaseRegistry()

    with pytest.raises(InvalidBrowserProfileIdError):
        registry.acquire_maintenance_many(
            [OTHER_PROFILE_ID, PROFILE_ID.upper()],
            _use("portable-restore"),
        )

    assert registry.snapshot(OTHER_PROFILE_ID) is None


def test_many_maintenance_scope_releases_in_reverse_sorted_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BrowserProfileLeaseRegistry()
    scope = registry.acquire_maintenance_many(
        [PROFILE_ID, OTHER_PROFILE_ID, THIRD_PROFILE_ID],
        _use("portable-export"),
    )
    released: list[str] = []
    original = registry._release_primary  # noqa: SLF001

    def track(
        profile_id: str,
        generation: int,
        token: str,
        mode: str,
    ) -> bool:
        released.append(profile_id)
        return original(  # type: ignore[arg-type]
            profile_id,
            generation,
            token,
            mode,
        )

    monkeypatch.setattr(registry, "_release_primary", track)
    with scope:
        scope.ensure_live()

    assert released == list(reversed(sorted({PROFILE_ID, OTHER_PROFILE_ID, THIRD_PROFILE_ID})))
    assert all(registry.snapshot(profile_id) is None for profile_id in scope.profile_ids)


def test_many_maintenance_reports_cleanup_failure_while_unwinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BrowserProfileLeaseRegistry()
    held = registry.acquire_owner(PROFILE_ID, _use("run"))
    original = MaintenanceProfileLease.release

    def release_but_report_failure(lease: MaintenanceProfileLease) -> bool:
        assert original(lease)
        return False

    monkeypatch.setattr(MaintenanceProfileLease, "release", release_but_report_failure)
    with pytest.raises(BrowserProfileInUseError):
        registry.acquire_maintenance_many(
            [PROFILE_ID, OTHER_PROFILE_ID],
            _use("portable-restore"),
        )

    assert registry.snapshot(OTHER_PROFILE_ID) is None
    held.release()
