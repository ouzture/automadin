"""Owner-thread page tracking for one Playwright browser context.

The registry deliberately avoids importing Playwright.  It accepts structural
page/context objects, keeps raw page objects on their creation thread, and
exposes immutable metadata snapshots for thread-safe session facades.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


class BrowserPageRegistryError(RuntimeError):
    """Base error for browser-page tracking failures."""


class BrowserPageThreadError(BrowserPageRegistryError):
    """The registry was accessed outside its browser owner thread."""


class BrowserNoActivePageError(BrowserPageRegistryError):
    """No live browser page can receive the requested operation."""


class BrowserTabNotFoundError(BrowserPageRegistryError):
    """A requested session-local tab ID is no longer live."""


class BrowserPageTargetError(BrowserPageRegistryError):
    """A durable page rule is invalid, ambiguous, or has no live match."""


class BrowserLastTabError(BrowserPageRegistryError):
    """The current tab cannot be closed because it is the only live tab."""


class BrowserPageCloseError(BrowserPageRegistryError):
    """One or more browser pages could not be confirmed closed."""

    def __init__(
        self,
        message: str,
        *,
        failures: tuple[BaseException, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failures = failures


class BrowserPageSnapshotUnstableError(BrowserPageRegistryError):
    """The page set changed continuously while a snapshot was collected."""


@dataclass(frozen=True)
class BrowserTabSnapshot:
    """Safe, immutable metadata for one tracked browser page."""

    tab_id: str
    index: int
    url: str
    title: str
    is_active: bool
    opener_tab_id: str | None


@dataclass(frozen=True)
class BrowserTabsSnapshot:
    """Safe, immutable state for all live pages in one browser context."""

    tabs: tuple[BrowserTabSnapshot, ...]
    active_tab_id: str | None


@dataclass(frozen=True)
class BrowserTabCloseResult:
    """Truthful outcome of closing the active browser target."""

    closed_tab_id: str
    opened_replacement: bool


@dataclass(frozen=True)
class BrowserPageEnsureResult:
    """Outcome of ensuring that one live page exists."""

    tab_id: str
    created: bool


@dataclass
class _TrackedPage:
    tab_id: str
    page: Any
    opener_tab_id: str | None
    legacy_indexed: bool


class BrowserPageRegistry:
    """Track pages and the logical Automadin target on the browser owner thread.

    ``tab_id`` values are stable only for this registry's lifetime.  They are
    suitable for an interactive tab picker, but must not be persisted in flows.
    """

    def __init__(self, context: Any) -> None:
        self._context = context
        self._owner_thread_id = threading.get_ident()
        self._tracked: list[_TrackedPage] = []
        self._tab_id_by_page_identity: dict[int, str] = {}
        self._active_tab_id: str | None = None
        self._next_tab_number = 1
        self._revision = 0

        context.on("page", self._on_context_page)
        self._reconcile(seed=True)

    @property
    def active_tab_id(self) -> str | None:
        """Return the stable ID of the logical Automadin target, if any."""
        self._assert_owner_thread()
        self._reconcile()
        return self._active_tab_id

    @property
    def active_page(self) -> Any:
        """Return the active raw page on the owner thread.

        Raw pages intentionally never appear in snapshots or facade APIs.
        """
        self._assert_owner_thread()
        self._reconcile()
        entry = self._entry_for_tab_id(self._active_tab_id)
        if entry is None:
            raise BrowserNoActivePageError(
                "No live browser tab is available. Open a tab or reconnect the browser."
            )
        return entry.page

    def track_page(self, page: Any, *, activate: bool | None = None) -> str:
        """Register ``page`` once and optionally make it the active target.

        Passing ``activate=True`` is intended for Automadin's explicit
        ``open_new_tab`` operation.  With ``activate=None``, only a popup whose
        opener is the current active page takes focus; unrelated background
        pages are tracked without stealing the target.
        """
        self._assert_owner_thread()
        return self._register_page(
            page,
            activate=activate,
            seed=False,
            legacy_indexed=True,
        )

    def select_tab(self, tab_id: str) -> None:
        """Select a live tab by stable session-local ID and bring it forward."""
        self._assert_owner_thread()
        self._reconcile()
        entry = self._entry_for_tab_id(tab_id)
        if entry is None:
            raise BrowserTabNotFoundError(
                f"Browser tab '{tab_id}' is no longer available."
            )
        entry.page.bring_to_front()
        # A synchronous Playwright call can dispatch a close callback before it
        # returns.  Never reactivate a tab that disappeared reentrantly.
        self._reconcile()
        if self._entry_for_tab_id(tab_id) is None:
            raise BrowserTabNotFoundError(
                f"Browser tab '{tab_id}' closed while it was being selected."
            )
        self._set_active_tab(tab_id)

    def select_matching_page(
        self,
        strategy: str,
        value: str = "",
        *,
        match_case: bool = False,
    ) -> str:
        """Atomically select a page using a durable relationship or metadata rule."""
        normalized_strategy = str(strategy).strip().casefold()
        allowed = {
            "newest",
            "newest_popup",
            "newest_child",
            "opener",
            "url_exact",
            "url_contains",
            "title_exact",
            "title_contains",
        }
        if normalized_strategy not in allowed:
            raise BrowserPageTargetError(
                f"Unsupported browser page strategy '{strategy}'."
            )
        self._reconcile()
        active = self._entry_for_tab_id(self._active_tab_id)
        candidate: _TrackedPage | None = None

        if normalized_strategy == "newest":
            candidate = self._tracked[-1] if self._tracked else None
        elif normalized_strategy == "newest_popup":
            candidate = next(
                (
                    entry
                    for entry in reversed(self._tracked)
                    if entry.opener_tab_id is not None
                ),
                None,
            )
        elif normalized_strategy == "newest_child":
            candidate = next(
                (
                    entry
                    for entry in reversed(self._tracked)
                    if active is not None
                    and entry.opener_tab_id == active.tab_id
                ),
                None,
            )
        elif normalized_strategy == "opener":
            candidate = self._entry_for_tab_id(
                active.opener_tab_id if active is not None else None
            )
        else:
            needle = str(value)
            if not needle:
                raise BrowserPageTargetError(
                    f"Browser page strategy '{normalized_strategy}' requires a value."
                )
            if not match_case:
                needle = needle.casefold()
            use_title = normalized_strategy.startswith("title_")
            exact = normalized_strategy.endswith("_exact")
            matches: list[_TrackedPage] = []
            for entry in tuple(self._tracked):
                haystack = (
                    self._safe_page_title(entry.page)
                    if use_title
                    else self._safe_page_url(entry.page)
                )
                if not match_case:
                    haystack = haystack.casefold()
                matched = haystack == needle if exact else needle in haystack
                if matched:
                    matches.append(entry)
            self._reconcile()
            matches = [
                entry
                for entry in matches
                if self._entry_for_tab_id(entry.tab_id) is not None
            ]
            if len(matches) > 1:
                raise BrowserPageTargetError(
                    f"Browser page rule matched {len(matches)} tabs; make it "
                    "more specific."
                )
            candidate = matches[0] if matches else None

        if candidate is None:
            raise BrowserPageTargetError(
                f"No live browser tab matched '{normalized_strategy}'."
            )
        self.select_tab(candidate.tab_id)
        return candidate.tab_id

    def ensure_page(
        self,
        url: str | None = None,
        *,
        navigation_timeout_ms: int = 30000,
    ) -> BrowserPageEnsureResult:
        """Keep an existing active page or create one when every page is closed."""
        self._assert_owner_thread()
        self._reconcile()
        active = self._entry_for_tab_id(self._active_tab_id)
        if active is not None:
            return BrowserPageEnsureResult(active.tab_id, created=False)
        try:
            page = self._context.new_page()
            tab_id = self.track_page(page, activate=True)
            if url:
                page.goto(
                    url,
                    wait_until="commit",
                    timeout=navigation_timeout_ms,
                )
        except Exception as exc:
            raise BrowserNoActivePageError(
                "No live browser page could be created. The browser may have "
                "closed; reconnect it and try again."
            ) from exc
        return BrowserPageEnsureResult(tab_id, created=True)

    def discard_page(self, page: Any) -> None:
        """Forget a page that Playwright has confirmed can no longer be used.

        Playwright can report a closed target a moment before its ``close``
        event is delivered.  Removing that target lets a recovery operation
        create a replacement instead of selecting the stale page again.
        """
        self._assert_owner_thread()
        self._remove_page(page)

    def close_active_page(self) -> BrowserTabCloseResult:
        """Close the current target while preserving at least one live tab."""
        self._assert_owner_thread()
        self._reconcile()
        if not self._tracked:
            raise BrowserNoActivePageError(
                "No live browser tab is available. Open a tab or reconnect the browser."
            )
        if len(self._tracked) == 1:
            raise BrowserLastTabError(
                "The only browser tab cannot be closed. Use Close All Tabs to "
                "replace it with a fresh tab."
            )
        entry = self._entry_for_tab_id(self._active_tab_id)
        if entry is None:  # pragma: no cover - guarded by reconciliation
            raise BrowserNoActivePageError(
                "No live browser tab is available. Open a tab or reconnect the browser."
            )
        close_error: BaseException | None = None
        try:
            entry.page.close()
        except Exception as exc:
            close_error = exc
        self._reconcile()
        if self._entry_for_page(entry.page) is not None:
            failure = BrowserPageCloseError(
                f"Browser tab '{entry.tab_id}' could not be confirmed closed.",
                failures=((close_error,) if close_error is not None else ()),
            )
            raise failure from close_error
        opened_replacement = False
        if not self._tracked:
            try:
                fresh_page = self._context.new_page()
                self.track_page(fresh_page, activate=True)
                opened_replacement = True
            except Exception as exc:
                raise BrowserPageCloseError(
                    "The current browser tab closed, but no replacement tab "
                    "could be opened.",
                    failures=(exc,),
                ) from exc
        return BrowserTabCloseResult(
            closed_tab_id=entry.tab_id,
            opened_replacement=opened_replacement,
        )

    def replace_all_with_fresh_page(self) -> str:
        """Open one fresh page, then close every page that existed before it."""
        self._assert_owner_thread()
        self._reconcile()
        previous_pages = tuple(entry.page for entry in self._tracked)
        fresh_page = self._context.new_page()
        fresh_tab_id = self.track_page(fresh_page, activate=True)
        close_failures: list[BaseException] = []
        for page in previous_pages:
            try:
                page.close()
            except Exception as exc:
                # Reconciliation below is authoritative: an API call can report
                # an error after the page has nevertheless disappeared.
                close_failures.append(exc)
        self._reconcile()
        survivors = [
            entry
            for entry in self._tracked
            if entry.tab_id != fresh_tab_id
        ]
        if self._entry_for_tab_id(fresh_tab_id) is None:
            raise BrowserPageCloseError(
                "The fresh browser tab closed unexpectedly while resetting tabs."
            )
        self._set_active_tab(fresh_tab_id)
        if survivors:
            survivor_ids = ", ".join(entry.tab_id for entry in survivors)
            failure = BrowserPageCloseError(
                "Not every previous browser tab could be confirmed closed "
                f"({len(survivors)} remaining: {survivor_ids}).",
                failures=tuple(close_failures),
            )
            raise failure from (close_failures[0] if close_failures else None)
        return fresh_tab_id

    def tab_id_at(self, index: int) -> str:
        """Resolve the legacy zero-based position used by persisted flows.

        Before popup tracking, numeric indices included only startup pages and
        tabs explicitly opened by Automadin.  Discovered site/user pages stay
        out of this projection so existing saved indices cannot silently drift.
        """
        self._assert_owner_thread()
        self._reconcile()
        legacy_tabs = [
            entry for entry in self._tracked if entry.legacy_indexed
        ]
        if index < 0 or index >= len(legacy_tabs):
            raise BrowserTabNotFoundError(
                f"Browser tab index {index} is not available."
            )
        return legacy_tabs[index].tab_id

    def snapshot(self) -> BrowserTabsSnapshot:
        """Return immutable page metadata without exposing browser objects."""
        self._assert_owner_thread()
        for _attempt in range(3):
            self._reconcile()
            starting_revision = self._revision
            active_tab_id = self._active_tab_id
            entries = tuple(self._tracked)
            tabs = tuple(
                BrowserTabSnapshot(
                    tab_id=entry.tab_id,
                    index=index,
                    url=self._safe_page_url(entry.page),
                    title=self._safe_page_title(entry.page),
                    is_active=entry.tab_id == active_tab_id,
                    opener_tab_id=entry.opener_tab_id,
                )
                for index, entry in enumerate(entries)
            )
            # Metadata reads are synchronous Playwright calls and can pump page
            # or close callbacks.  Return only one coherent post-reconcile view.
            self._reconcile()
            if (
                self._revision == starting_revision
                and self._active_tab_id == active_tab_id
            ):
                return BrowserTabsSnapshot(
                    tabs=tabs,
                    active_tab_id=active_tab_id,
                )
        raise BrowserPageSnapshotUnstableError(
            "Browser tabs kept changing while their details were read. Try again."
        )

    def reconcile(self) -> None:
        """Synchronize with the context's authoritative live-page collection."""
        self._assert_owner_thread()
        self._reconcile()

    def _on_context_page(self, page: Any) -> None:
        self._assert_owner_thread()
        self._register_page(
            page,
            activate=None,
            seed=False,
            legacy_indexed=False,
        )

    def _on_page_popup(self, parent_page: Any, popup: Any) -> None:
        self._assert_owner_thread()
        parent_tab_id = self._tab_id_by_page_identity.get(id(parent_page))
        self._register_page(
            popup,
            activate=(
                parent_tab_id is not None
                and parent_tab_id == self._active_tab_id
            ),
            seed=False,
            legacy_indexed=False,
            known_opener_tab_id=parent_tab_id,
        )

    def _on_page_close(self, page: Any) -> None:
        self._assert_owner_thread()
        self._remove_page(page)

    def _reconcile(self, *, seed: bool = False) -> None:
        context_pages = [
            page for page in list(self._context.pages) if not self._page_is_closed(page)
        ]
        live_page_identities = {id(page) for page in context_pages}

        for entry in list(self._tracked):
            if id(entry.page) not in live_page_identities:
                self._remove_page(entry.page)

        for page in context_pages:
            already_tracked = id(page) in self._tab_id_by_page_identity
            self._register_page(
                page,
                activate=False if seed or already_tracked else None,
                seed=seed,
                legacy_indexed=seed,
            )

        if self._entry_for_tab_id(self._active_tab_id) is None:
            self._set_active_tab(None)
        if self._active_tab_id is None and self._tracked:
            self._set_active_tab(self._tracked[0].tab_id)

    def _register_page(
        self,
        page: Any,
        *,
        activate: bool | None,
        seed: bool,
        legacy_indexed: bool,
        known_opener_tab_id: str | None = None,
    ) -> str:
        page_identity = id(page)
        existing_tab_id = self._tab_id_by_page_identity.get(page_identity)
        if existing_tab_id is not None:
            return self._merge_existing_registration(
                existing_tab_id,
                activate=activate,
                legacy_indexed=legacy_indexed,
                opener_tab_id=known_opener_tab_id,
            )

        opener_tab_id = known_opener_tab_id or self._opener_tab_id(page)
        # Reading opener is a synchronous Playwright call and may dispatch the
        # parent page's popup callback, which can register this same page.
        existing_tab_id = self._tab_id_by_page_identity.get(page_identity)
        if existing_tab_id is not None:
            return self._merge_existing_registration(
                existing_tab_id,
                activate=activate,
                legacy_indexed=legacy_indexed,
                opener_tab_id=opener_tab_id,
            )
        tab_id = f"tab-{self._next_tab_number}"
        self._next_tab_number += 1
        self._tracked.append(
            _TrackedPage(
                tab_id=tab_id,
                page=page,
                opener_tab_id=opener_tab_id,
                legacy_indexed=legacy_indexed,
            )
        )
        self._tab_id_by_page_identity[page_identity] = tab_id
        page.on("close", lambda *_args, tracked_page=page: self._on_page_close(tracked_page))
        page.on(
            "popup",
            lambda popup, tracked_page=page: self._on_page_popup(
                tracked_page,
                popup,
            ),
        )
        self._revision += 1

        should_activate = activate
        if should_activate is None:
            should_activate = (
                opener_tab_id is not None
                and opener_tab_id == self._active_tab_id
            )
        if should_activate or (not seed and self._active_tab_id is None):
            self._set_active_tab(tab_id)
        return tab_id

    def _merge_existing_registration(
        self,
        tab_id: str,
        *,
        activate: bool | None,
        legacy_indexed: bool,
        opener_tab_id: str | None,
    ) -> str:
        existing = self._entry_for_tab_id(tab_id)
        if existing is None:
            raise BrowserPageRegistryError(
                f"Browser tab '{tab_id}' lost its tracked page identity."
            )
        changed = False
        if legacy_indexed and not existing.legacy_indexed:
            existing.legacy_indexed = True
            changed = True
        if opener_tab_id is not None and existing.opener_tab_id is None:
            existing.opener_tab_id = opener_tab_id
            changed = True
        if changed:
            self._revision += 1
        should_activate = activate
        if should_activate is None:
            should_activate = (
                existing.opener_tab_id is not None
                and existing.opener_tab_id == self._active_tab_id
            )
        if should_activate:
            self._set_active_tab(tab_id)
        return tab_id

    def _remove_page(self, page: Any) -> None:
        tab_id = self._tab_id_by_page_identity.pop(id(page), None)
        if tab_id is None:
            return
        removed_index = next(
            index
            for index, entry in enumerate(self._tracked)
            if entry.tab_id == tab_id
        )
        removed_entry = self._tracked.pop(removed_index)
        was_active = self._active_tab_id == tab_id
        self._revision += 1

        if not was_active:
            return
        fallback_tab_id = None
        if self._entry_for_tab_id(removed_entry.opener_tab_id) is not None:
            fallback_tab_id = removed_entry.opener_tab_id
        elif self._tracked:
            nearest_index = min(removed_index, len(self._tracked) - 1)
            fallback_tab_id = self._tracked[nearest_index].tab_id
        self._set_active_tab(fallback_tab_id)

    def _opener_tab_id(self, page: Any) -> str | None:
        try:
            opener = getattr(page, "opener", None)
            if callable(opener):
                opener = opener()
        except Exception:
            return None
        if opener is None:
            return None
        return self._tab_id_by_page_identity.get(id(opener))

    def _set_active_tab(self, tab_id: str | None) -> None:
        if tab_id == self._active_tab_id:
            return
        self._active_tab_id = tab_id
        self._revision += 1

    def _entry_for_tab_id(self, tab_id: str | None) -> _TrackedPage | None:
        if tab_id is None:
            return None
        return next(
            (entry for entry in self._tracked if entry.tab_id == tab_id),
            None,
        )

    def _entry_for_page(self, page: Any) -> _TrackedPage | None:
        return next(
            (entry for entry in self._tracked if entry.page is page),
            None,
        )

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise BrowserPageThreadError(
                "Browser pages can only be accessed on their owner thread."
            )

    @staticmethod
    def _page_is_closed(page: Any) -> bool:
        try:
            is_closed = getattr(page, "is_closed", None)
            return bool(is_closed()) if callable(is_closed) else bool(is_closed)
        except Exception:
            # An indeterminate close state must remain tracked.  Treating an
            # inspection failure as closed could silently switch targets or
            # report successful cleanup while a live page survives.
            return False

    @staticmethod
    def _safe_page_url(page: Any) -> str:
        try:
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _safe_page_title(page: Any) -> str:
        try:
            title = getattr(page, "title", "")
            return str(title() if callable(title) else title or "")
        except Exception:
            return ""
