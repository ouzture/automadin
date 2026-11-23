"""In-process ownership coordination for managed browser profiles.

Persistent browser profile directories must not be opened by two independent
browser contexts or changed while a browser owns them.  This module provides a
small, non-blocking registry for that lifecycle.  It deliberately coordinates
only this Automadin process; an OS-level lock can be layered on later if
multiple application processes must share the same profile root.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from automadin.services.browser_profile_paths import canonical_profile_id

PrimaryLeaseMode = Literal["owner", "maintenance"]
LeaseRequestMode = Literal["owner", "maintenance", "borrow"]


@dataclass(frozen=True, slots=True)
class ProfileUse:
    """Human-readable metadata identifying one operation using a profile."""

    kind: str
    label: str
    operation_id: str

    def __post_init__(self) -> None:
        for field_name in ("kind", "label", "operation_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Profile use {field_name} must be non-empty text.")


@dataclass(frozen=True, slots=True)
class ProfileLeaseSnapshot:
    """Immutable diagnostic view of the currently held lease."""

    profile_id: str
    generation: int
    mode: PrimaryLeaseMode
    use: ProfileUse
    borrowed_by: ProfileUse | None = None
    closing: bool = False


@dataclass(frozen=True, slots=True)
class ProfileLeaseRequestSnapshot:
    """Immutable diagnostic view of a rejected lease request."""

    profile_id: str
    mode: LeaseRequestMode
    use: ProfileUse
    owner_generation: int | None = None


class BrowserProfileLeaseError(RuntimeError):
    """Base class for managed browser-profile lease failures."""


class BrowserProfileInUseError(BrowserProfileLeaseError):
    """Raised when a non-blocking request conflicts with a current lease."""

    def __init__(
        self,
        current: ProfileLeaseSnapshot,
        requested: ProfileLeaseRequestSnapshot,
    ) -> None:
        self.current = current
        self.requested = requested
        detail = f" by {current.borrowed_by.label}" if current.borrowed_by else ""
        super().__init__(
            f"Browser profile {current.profile_id} is already in use by "
            f"{current.use.label}{detail}; cannot start {requested.use.label}."
        )


class BrowserProfileBorrowActiveError(BrowserProfileLeaseError):
    """Raised when an owner is closed while its exclusive borrow is active."""

    def __init__(self, current: ProfileLeaseSnapshot) -> None:
        self.current = current
        borrowed_by = current.borrowed_by
        label = borrowed_by.label if borrowed_by is not None else "another operation"
        super().__init__(
            f"Browser profile {current.profile_id} cannot be released while "
            f"{label} is borrowing its live browser session."
        )


class BrowserProfileLeaseStateError(BrowserProfileLeaseError):
    """Raised when a lease handle no longer identifies a live owner."""

    def __init__(self, profile_id: str, reason: str) -> None:
        self.profile_id = profile_id
        self.reason = reason
        super().__init__(f"Browser profile {profile_id} lease is not usable: {reason}.")


class BrowserProfileLeaseCleanupError(BrowserProfileLeaseError):
    """Raised when an acquired maintenance lease could not be released."""

    def __init__(self, scope: MaintenanceProfileScope) -> None:
        self.scope = scope
        self.profile_ids = scope.profile_ids
        joined = ", ".join(self.profile_ids)
        super().__init__(
            "Browser profile maintenance ownership could not be released for: "
            f"{joined}."
        )


@dataclass(slots=True)
class _Entry:
    generation: int
    mode: PrimaryLeaseMode
    use: ProfileUse
    token: str
    borrow_use: ProfileUse | None = None
    borrow_token: str | None = None
    closing: bool = False

    def snapshot(self, profile_id: str) -> ProfileLeaseSnapshot:
        return ProfileLeaseSnapshot(
            profile_id=profile_id,
            generation=self.generation,
            mode=self.mode,
            use=self.use,
            borrowed_by=self.borrow_use,
            closing=self.closing,
        )


class BrowserProfileLeaseRegistry:
    """Thread-safe, non-blocking registry keyed by canonical profile UUID."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, _Entry] = {}
        self._generations: dict[str, int] = {}

    def acquire_owner(
        self,
        profile_id: str | UUID,
        use: ProfileUse,
    ) -> OwnerProfileLease:
        canonical_id = canonical_profile_id(profile_id)
        generation, token = self._acquire_primary(canonical_id, "owner", use)
        return OwnerProfileLease(self, canonical_id, generation, token, use)

    def acquire_maintenance(
        self,
        profile_id: str | UUID,
        use: ProfileUse,
    ) -> MaintenanceProfileLease:
        canonical_id = canonical_profile_id(profile_id)
        generation, token = self._acquire_primary(canonical_id, "maintenance", use)
        return MaintenanceProfileLease(self, canonical_id, generation, token, use)

    def acquire_maintenance_many(
        self,
        profile_ids: Iterable[str | UUID],
        use: ProfileUse,
    ) -> MaintenanceProfileScope:
        """Acquire an all-or-nothing sorted maintenance scope.

        Canonicalization completes before ownership changes. Duplicate IDs are
        collapsed, acquisition is deterministic, and a conflict unwinds every
        lease acquired by this request in reverse order.
        """
        canonical_ids = tuple(
            sorted({canonical_profile_id(profile_id) for profile_id in profile_ids})
        )
        leases: list[MaintenanceProfileLease] = []
        try:
            for profile_id in canonical_ids:
                leases.append(self.acquire_maintenance(profile_id, use))
        except Exception as exc:
            partial_scope = MaintenanceProfileScope(self, tuple(leases))
            try:
                partial_scope.release()
            except BrowserProfileLeaseCleanupError as cleanup_exc:
                raise cleanup_exc from exc
            raise
        return MaintenanceProfileScope(self, tuple(leases))

    def validate_maintenance(
        self,
        lease: object,
        expected_profile_id: str | UUID,
    ) -> MaintenanceProfileLease:
        """Validate an exact live maintenance capability minted by this registry."""
        canonical_id = canonical_profile_id(expected_profile_id)
        if not isinstance(lease, MaintenanceProfileLease):
            raise TypeError("A maintenance profile lease is required.")
        if lease._registry is not self:
            raise BrowserProfileLeaseStateError(
                canonical_id,
                "the maintenance lease belongs to a different registry",
            )
        lease.ensure_live(canonical_id)
        return lease

    def acquire_borrow(
        self,
        owner: OwnerProfileLease,
        use: ProfileUse,
    ) -> BorrowedProfileLease:
        if not isinstance(owner, OwnerProfileLease) or owner._registry is not self:
            raise ValueError("A borrow requires a live owner from this lease registry.")
        profile_id = owner.profile_id
        request = ProfileLeaseRequestSnapshot(
            profile_id=profile_id,
            mode="borrow",
            use=use,
            owner_generation=owner.generation,
        )
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.mode != "owner"
                or entry.generation != owner.generation
                or entry.token != owner._token
                or entry.closing
            ):
                current = (
                    entry.snapshot(profile_id)
                    if entry is not None
                    else ProfileLeaseSnapshot(
                        profile_id=profile_id,
                        generation=owner.generation,
                        mode="owner",
                        use=owner.use,
                    )
                )
                raise BrowserProfileInUseError(current, request)
            if entry.borrow_token is not None:
                raise BrowserProfileInUseError(entry.snapshot(profile_id), request)
            borrow_token = str(uuid4())
            entry.borrow_token = borrow_token
            entry.borrow_use = use
        return BorrowedProfileLease(
            self,
            profile_id,
            owner.generation,
            owner._token,
            borrow_token,
            use,
        )

    def snapshot(self, profile_id: str | UUID) -> ProfileLeaseSnapshot | None:
        canonical_id = canonical_profile_id(profile_id)
        with self._lock:
            entry = self._entries.get(canonical_id)
            return entry.snapshot(canonical_id) if entry is not None else None

    def _acquire_primary(
        self,
        profile_id: str,
        mode: PrimaryLeaseMode,
        use: ProfileUse,
    ) -> tuple[int, str]:
        request = ProfileLeaseRequestSnapshot(profile_id, mode, use)
        with self._lock:
            current = self._entries.get(profile_id)
            if current is not None:
                raise BrowserProfileInUseError(current.snapshot(profile_id), request)
            generation = self._generations.get(profile_id, 0) + 1
            self._generations[profile_id] = generation
            token = str(uuid4())
            self._entries[profile_id] = _Entry(generation, mode, use, token)
            return generation, token

    def _release_primary(
        self,
        profile_id: str,
        generation: int,
        token: str,
        mode: PrimaryLeaseMode,
    ) -> bool:
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.generation != generation
                or entry.token != token
                or entry.mode != mode
            ):
                return False
            if entry.borrow_token is not None:
                raise BrowserProfileBorrowActiveError(entry.snapshot(profile_id))
            del self._entries[profile_id]
            return True

    def _ensure_owner_live(
        self,
        profile_id: str,
        generation: int,
        token: str,
        *,
        allow_closing: bool = False,
    ) -> ProfileLeaseSnapshot:
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.mode != "owner"
                or entry.generation != generation
                or entry.token != token
            ):
                raise BrowserProfileLeaseStateError(
                    profile_id,
                    "the owner was released or superseded",
                )
            if entry.closing and not allow_closing:
                raise BrowserProfileLeaseStateError(profile_id, "the owner is closing")
            return entry.snapshot(profile_id)

    def _ensure_maintenance_live(
        self,
        profile_id: str,
        generation: int,
        token: str,
    ) -> ProfileLeaseSnapshot:
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.mode != "maintenance"
                or entry.generation != generation
                or entry.token != token
            ):
                raise BrowserProfileLeaseStateError(
                    profile_id,
                    "the maintenance lease was released or superseded",
                )
            return entry.snapshot(profile_id)

    def _begin_owner_close(self, profile_id: str, generation: int, token: str) -> bool:
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.mode != "owner"
                or entry.generation != generation
                or entry.token != token
            ):
                raise BrowserProfileLeaseStateError(
                    profile_id,
                    "the owner was released or superseded",
                )
            if entry.borrow_token is not None:
                raise BrowserProfileBorrowActiveError(entry.snapshot(profile_id))
            if entry.closing:
                return False
            entry.closing = True
            return True

    def _cancel_owner_close(self, profile_id: str, generation: int, token: str) -> bool:
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.mode != "owner"
                or entry.generation != generation
                or entry.token != token
                or not entry.closing
            ):
                return False
            entry.closing = False
            return True

    def _release_borrow(
        self,
        profile_id: str,
        generation: int,
        owner_token: str,
        borrow_token: str,
    ) -> bool:
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.mode != "owner"
                or entry.generation != generation
                or entry.token != owner_token
                or entry.borrow_token != borrow_token
            ):
                return False
            entry.borrow_token = None
            entry.borrow_use = None
            return True

    def _ensure_borrow_live(
        self,
        profile_id: str,
        generation: int,
        owner_token: str,
        borrow_token: str,
    ) -> ProfileLeaseSnapshot:
        with self._lock:
            entry = self._entries.get(profile_id)
            if (
                entry is None
                or entry.mode != "owner"
                or entry.generation != generation
                or entry.token != owner_token
                or entry.borrow_token != borrow_token
            ):
                raise BrowserProfileLeaseStateError(
                    profile_id,
                    "the borrowed owner was released or superseded",
                )
            return entry.snapshot(profile_id)


class _PrimaryProfileLease:
    _mode: PrimaryLeaseMode

    def __init__(
        self,
        registry: BrowserProfileLeaseRegistry,
        profile_id: str,
        generation: int,
        token: str,
        use: ProfileUse,
    ) -> None:
        self._registry = registry
        self.profile_id = profile_id
        self.generation = generation
        self._token = token
        self.use = use
        self._released = False
        self._state_lock = RLock()

    @property
    def released(self) -> bool:
        with self._state_lock:
            return self._released

    def release(self) -> bool:
        with self._state_lock:
            if self._released:
                return False
            released = self._registry._release_primary(
                self.profile_id,
                self.generation,
                self._token,
                self._mode,
            )
            # A stale token is terminal for this handle too.  A borrow-active
            # error is raised before this line, leaving the owner retryable.
            self._released = True
            return released

    close = release

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class OwnerProfileLease(_PrimaryProfileLease):
    """Exclusive owner of one persistent browser-profile directory."""

    _mode: PrimaryLeaseMode = "owner"

    def borrow(self, use: ProfileUse) -> BorrowedProfileLease:
        return self._registry.acquire_borrow(self, use)

    def ensure_live(self, expected_profile_id: str | UUID | None = None) -> None:
        """Reject a released, stale, closing, or profile-mismatched owner."""
        if expected_profile_id is not None:
            expected = canonical_profile_id(expected_profile_id)
            if expected != self.profile_id:
                raise BrowserProfileLeaseStateError(
                    self.profile_id,
                    f"it belongs to a different profile than {expected}",
                )
        if self.released:
            raise BrowserProfileLeaseStateError(self.profile_id, "the owner was released")
        self._registry._ensure_owner_live(
            self.profile_id,
            self.generation,
            self._token,
        )

    def begin_close(self) -> bool:
        """Atomically block new borrows before browser teardown begins."""
        if self.released:
            raise BrowserProfileLeaseStateError(self.profile_id, "the owner was released")
        return self._registry._begin_owner_close(
            self.profile_id,
            self.generation,
            self._token,
        )

    def cancel_close(self) -> bool:
        """Make the live owner borrowable again after aborted teardown."""
        if self.released:
            return False
        return self._registry._cancel_owner_close(
            self.profile_id,
            self.generation,
            self._token,
        )


class MaintenanceProfileLease(_PrimaryProfileLease):
    """Exclusive permission to modify one persistent profile directory."""

    _mode: PrimaryLeaseMode = "maintenance"

    def ensure_live(self, expected_profile_id: str | UUID | None = None) -> None:
        """Reject a released, stale, or profile-mismatched maintenance lease."""
        if expected_profile_id is not None:
            expected = canonical_profile_id(expected_profile_id)
            if expected != self.profile_id:
                raise BrowserProfileLeaseStateError(
                    self.profile_id,
                    f"it belongs to a different profile than {expected}",
                )
        if self.released:
            raise BrowserProfileLeaseStateError(
                self.profile_id,
                "the maintenance lease was released",
            )
        self._registry._ensure_maintenance_live(
            self.profile_id,
            self.generation,
            self._token,
        )


class MaintenanceProfileScope:
    """All-or-nothing ownership of a canonical, sorted set of profiles."""

    def __init__(
        self,
        registry: BrowserProfileLeaseRegistry,
        leases: tuple[MaintenanceProfileLease, ...],
    ) -> None:
        self._registry = registry
        self._leases = leases
        self._by_profile = {lease.profile_id: lease for lease in leases}
        self._released = False
        self._state_lock = RLock()

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(lease.profile_id for lease in self._leases)

    @property
    def released(self) -> bool:
        with self._state_lock:
            return self._released

    def for_profile(
        self,
        profile_id: str | UUID,
    ) -> MaintenanceProfileLease:
        canonical_id = canonical_profile_id(profile_id)
        with self._state_lock:
            if self._released:
                raise BrowserProfileLeaseStateError(
                    canonical_id,
                    "the maintenance scope was released",
                )
            lease = self._by_profile.get(canonical_id)
            if lease is None:
                raise BrowserProfileLeaseStateError(
                    canonical_id,
                    "the profile is outside this maintenance scope",
                )
            if lease._registry is not self._registry:
                raise BrowserProfileLeaseStateError(
                    canonical_id,
                    "the maintenance lease belongs to a different registry",
                )
            lease.ensure_live(canonical_id)
            return lease

    def ensure_live(self) -> None:
        with self._state_lock:
            if self._released:
                profile_id = self.profile_ids[0] if self.profile_ids else "scope"
                raise BrowserProfileLeaseStateError(
                    profile_id,
                    "the maintenance scope was released",
                )
            for lease in self._leases:
                if lease._registry is not self._registry:
                    raise BrowserProfileLeaseStateError(
                        lease.profile_id,
                        "the maintenance lease belongs to a different registry",
                    )
                lease.ensure_live(lease.profile_id)

    def release(self) -> bool:
        with self._state_lock:
            if self._released:
                return False
            survivors: list[MaintenanceProfileLease] = []
            for lease in reversed(self._leases):
                try:
                    lease.release()
                except Exception:
                    try:
                        self._registry.validate_maintenance(lease, lease.profile_id)
                    except BrowserProfileLeaseStateError:
                        continue
                    survivors.append(lease)
            self._leases = tuple(reversed(survivors))
            self._by_profile = {lease.profile_id: lease for lease in self._leases}
            self._released = not self._leases
            if self._leases:
                raise BrowserProfileLeaseCleanupError(self)
            return True

    close = release

    def __enter__(self) -> MaintenanceProfileScope:
        self.ensure_live()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()


class BorrowedProfileLease:
    """Exclusive temporary borrower of an exact live owner generation."""

    def __init__(
        self,
        registry: BrowserProfileLeaseRegistry,
        profile_id: str,
        owner_generation: int,
        owner_token: str,
        token: str,
        use: ProfileUse,
    ) -> None:
        self._registry = registry
        self.profile_id = profile_id
        self.owner_generation = owner_generation
        self._owner_token = owner_token
        self._token = token
        self.use = use
        self._released = False
        self._state_lock = RLock()

    @property
    def released(self) -> bool:
        with self._state_lock:
            return self._released

    def release(self) -> bool:
        with self._state_lock:
            if self._released:
                return False
            released = self._registry._release_borrow(
                self.profile_id,
                self.owner_generation,
                self._owner_token,
                self._token,
            )
            self._released = True
            return released

    def ensure_live(self, owner: OwnerProfileLease | None = None) -> None:
        """Reject a released/stale borrow or one from a different owner."""
        if owner is not None and (
            owner._registry is not self._registry
            or owner.profile_id != self.profile_id
            or owner.generation != self.owner_generation
            or owner._token != self._owner_token
        ):
            raise BrowserProfileLeaseStateError(
                self.profile_id,
                "the borrow belongs to a different browser owner",
            )
        if self.released:
            raise BrowserProfileLeaseStateError(
                self.profile_id,
                "the borrow was released",
            )
        self._registry._ensure_borrow_live(
            self.profile_id,
            self.owner_generation,
            self._owner_token,
            self._token,
        )

    close = release

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


# Process-wide default for production wiring; tests and composed services can
# inject their own registry to stay deterministic and isolated.
default_browser_profile_lease_registry = BrowserProfileLeaseRegistry()
browser_profile_leases = default_browser_profile_lease_registry
