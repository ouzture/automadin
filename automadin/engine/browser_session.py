"""Thread-owned interactive browser sessions.

Playwright's synchronous API binds its greenlet to the thread that starts it.
Interactive sessions therefore keep creation, page operations, and teardown on
one dedicated owner thread.  Callers receive this handle instead of raw
Playwright objects.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from automadin.capabilities.browser.interaction import (
    ElementInspection,
    ElementCollectionSnapshot,
    PickedElement,
    assert_element_visible,
    is_element_visible,
    click_element,
    count_elements,
    click_snapshot_element,
    extract_element_text,
    extract_snapshot_element_text,
    fill_element,
    inspect_element,
    poll_element_picker,
    release_element_snapshot,
    scroll_element_into_view,
    scroll_page,
    scroll_snapshot_element_into_view,
    snapshot_elements,
    start_element_picker,
    stop_element_picker,
    wait_for_element,
)
from automadin.engine.browser_engine import (
    BrowserDeviceEngine,
    BrowserSessionData,
    BrowserSessionTeardownError,
)
from automadin.engine.browser_companion import CompanionBrowserPort
from automadin.engine.browser_pages import BrowserTabsSnapshot
from automadin.models.device import BrowserDevice, DeviceSession
from automadin.models.browser_dom import BrowserDomScope
from automadin.models.locator import LocatorSpec
from automadin.services.browser_profile_leases import (
    BorrowedProfileLease,
    OwnerProfileLease,
    ProfileUse,
)

logger = logging.getLogger(__name__)

_DEFAULT_OPERATION_TIMEOUT_SECONDS = 30.0
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0
_DEFAULT_CLOSE_TIMEOUT_SECONDS = 30.0


class BrowserSessionOperationUncertainError(TimeoutError):
    """A mutating owner request started but did not finish before its deadline."""


class BrowserSessionPort(Protocol):
    """Safe browser capabilities exposed to code outside an owner."""

    def screenshot(self) -> bytes: ...

    def tabs_snapshot(self) -> BrowserTabsSnapshot: ...

    def select_tab(self, tab_id: str) -> None: ...

    def click_locator(
        self,
        locator: object,
        *,
        timeout_ms: int | None = None,
        anchor: str = "center",
        offset_x: float = 0,
        offset_y: float = 0,
        offset_unit: str = "pixels",
        require_new_child_tab: bool = False,
    ) -> None: ...

    def wait_for_locator(self, locator: object, *, timeout_ms: int = 5000) -> None: ...

    def fill_locator(
        self,
        locator: object,
        value: str,
        *,
        min_delay: int = 0,
        max_delay: int = 0,
    ) -> None: ...

    def extract_locator_text(self, locator: object) -> str: ...

    def extract_snapshot_locator_text(
        self, token: str, index: int, *, child_selector: str = ""
    ) -> str: ...

    def assert_locator_visible(self, locator: object) -> None: ...

    def is_locator_visible(self, locator: object) -> bool: ...

    def scroll_page(self, *, delta_x: int = 0, delta_y: int = 600) -> None: ...

    def scroll_locator_into_view(
        self, locator: object, *, timeout_ms: int = 5000
    ) -> None: ...

    def scroll_snapshot_locator_into_view(
        self, token: str, index: int, *, timeout_ms: int = 5000
    ) -> None: ...

    def inspect_locator(self, locator: object) -> ElementInspection: ...

    def count_locators(self, locator: object, *, scope: object = None) -> int: ...

    def snapshot_locators(
        self, locator: object, *, scope: object = None
    ) -> ElementCollectionSnapshot: ...

    def click_snapshot_locator(
        self,
        token: str,
        index: int,
        *,
        timeout_ms: int | None = None,
        anchor: str = "center",
        offset_x: float = 0,
        offset_y: float = 0,
        offset_unit: str = "pixels",
        require_new_child_tab: bool = False,
    ) -> None: ...

    def release_locator_snapshot(self, token: str) -> None: ...

    def start_element_picker(self) -> None: ...

    def poll_element_picker(self) -> PickedElement | None: ...

    def stop_element_picker(self) -> PickedElement | None: ...

    def execute_flow(
        self,
        executor: Any,
        flow: Any,
        ctx: Any,
        *,
        use: ProfileUse,
        borrowed_lease: BorrowedProfileLease | None = None,
        timeout: float | None = None,
    ) -> None: ...

    def execute_command(
        self,
        executor: Any,
        command: Any,
        ctx: Any,
        *,
        use: ProfileUse,
        borrowed_lease: BorrowedProfileLease | None = None,
        timeout: float | None = None,
    ) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _BrowserRequest:
    operation: Literal[
        "screenshot",
        "tabs_snapshot",
        "select_tab",
        "click_locator",
        "wait_for_locator",
        "fill_locator",
        "extract_locator_text",
        "extract_snapshot_locator_text",
        "assert_locator_visible",
        "is_locator_visible",
        "scroll_page",
        "scroll_locator_into_view",
        "scroll_snapshot_locator_into_view",
        "inspect_locator",
        "count_locators",
        "snapshot_locators",
        "click_snapshot_locator",
        "release_locator_snapshot",
        "start_element_picker",
        "poll_element_picker",
        "stop_element_picker",
        "execute_flow",
        "execute_command",
        "close",
    ]
    future: Future[Any]
    args: tuple[Any, ...] = ()
    borrow: BorrowedProfileLease | None = None
    release_borrow: bool = False


class BrowserSessionHandle:
    """Thread-safe request handle for one interactive browser session.

    The raw :class:`BrowserSessionData` never leaves the owner thread.  New
    interactive browser capabilities should be added as explicit methods and
    serialized requests here rather than exposing ``Page`` or
    ``BrowserContext`` to UI workers.
    """

    def __init__(
        self,
        device: BrowserDevice,
        engine: BrowserDeviceEngine,
        *,
        profile_lease: OwnerProfileLease | None = None,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._device = device
        self._engine = engine
        self._profile_lease = profile_lease
        self._requests: queue.Queue[_BrowserRequest] = queue.Queue()
        self._startup: Future[None] = Future()
        self._close_future: Future[None] = Future()
        self._state_lock = threading.Lock()
        self._closing = False
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run_owner,
            name=f"AutomadinBrowserSession-{device.id}",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            if self._profile_lease is not None:
                self._profile_lease.release()
            self._closed.set()
            raise

        try:
            self._startup.result(timeout=startup_timeout)
        except Exception:
            self._request_close()
            raise

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def profile_lease(self) -> OwnerProfileLease | None:
        """Return the exact persistent-profile owner for safe session borrowing."""
        return self._profile_lease

    def screenshot(
        self,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> bytes:
        """Capture the active page on the Playwright owner thread."""
        result = self._submit("screenshot", timeout=timeout)
        if not isinstance(result, bytes):
            raise TypeError("Browser screenshot did not return bytes")
        return result

    def tabs_snapshot(
        self,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> BrowserTabsSnapshot:
        """Read immutable tab metadata on the Playwright owner thread."""
        result = self._submit("tabs_snapshot", timeout=timeout)
        if not isinstance(result, BrowserTabsSnapshot):
            raise TypeError("Browser tab listing returned an invalid result")
        return result

    def select_tab(
        self,
        tab_id: str,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        """Select one stable session-local tab on the Playwright owner thread."""
        if not isinstance(tab_id, str) or not tab_id.strip():
            raise ValueError("Browser tab ID must be non-empty text")
        self._submit("select_tab", args=(tab_id,), timeout=timeout)

    def click_locator(
        self,
        locator: object,
        *,
        timeout_ms: int | None = None,
        anchor: str = "center",
        offset_x: float = 0,
        offset_y: float = 0,
        offset_unit: str = "pixels",
        require_new_child_tab: bool = False,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        """Click an element without exposing its Playwright Locator."""
        spec = LocatorSpec.parse(locator)
        self._submit(
            "click_locator",
            args=(
                spec,
                timeout_ms,
                anchor,
                offset_x,
                offset_y,
                offset_unit,
                require_new_child_tab,
            ),
            timeout=timeout,
        )

    def wait_for_locator(
        self,
        locator: object,
        *,
        timeout_ms: int = 5000,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        """Wait for a located element without exposing Playwright objects."""
        self._submit(
            "wait_for_locator",
            args=(LocatorSpec.parse(locator), timeout_ms),
            timeout=timeout,
        )

    def fill_locator(
        self,
        locator: object,
        value: str,
        *,
        min_delay: int = 0,
        max_delay: int = 0,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        """Fill a located field on the Playwright owner thread."""
        self._submit(
            "fill_locator",
            args=(LocatorSpec.parse(locator), value, min_delay, max_delay),
            timeout=timeout,
        )

    def extract_locator_text(
        self,
        locator: object,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> str:
        """Extract text from a located element on the owner thread."""
        result = self._submit(
            "extract_locator_text",
            args=(LocatorSpec.parse(locator),),
            timeout=timeout,
        )
        if not isinstance(result, str):
            raise TypeError("Browser text extraction returned a non-text result")
        return result

    def assert_locator_visible(
        self,
        locator: object,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        """Assert located-element visibility on the owner thread."""
        self._submit(
            "assert_locator_visible",
            args=(LocatorSpec.parse(locator),),
            timeout=timeout,
        )

    def is_locator_visible(
        self,
        locator: object,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> bool:
        """Probe located-element visibility once on the owner thread."""
        result = self._submit(
            "is_locator_visible",
            args=(LocatorSpec.parse(locator),),
            timeout=timeout,
        )
        if not isinstance(result, bool):
            raise TypeError("Browser visibility probe returned a non-boolean result")
        return result

    def scroll_page(
        self,
        *,
        delta_x: int = 0,
        delta_y: int = 600,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._submit("scroll_page", args=(delta_x, delta_y), timeout=timeout)

    def scroll_locator_into_view(
        self,
        locator: object,
        *,
        timeout_ms: int = 5000,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._submit(
            "scroll_locator_into_view",
            args=(LocatorSpec.parse(locator), timeout_ms),
            timeout=timeout,
        )

    def scroll_snapshot_locator_into_view(
        self,
        token: str,
        index: int,
        *,
        timeout_ms: int = 5000,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise ValueError("Browser locator snapshot token cannot be empty")
        self._submit(
            "scroll_snapshot_locator_into_view",
            args=(token, index, timeout_ms),
            timeout=timeout,
        )

    def inspect_locator(
        self,
        locator: object,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> ElementInspection:
        """Inspect element matches on the Playwright owner thread."""
        result = self._submit(
            "inspect_locator",
            args=(LocatorSpec.parse(locator),),
            timeout=timeout,
        )
        if not isinstance(result, ElementInspection):
            raise TypeError("Browser locator inspection returned an invalid result")
        return result

    def extract_snapshot_locator_text(
        self,
        token: str,
        index: int,
        *,
        child_selector: str = "",
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> str:
        """Extract text from one retained collection element on the owner thread."""
        if not token:
            raise ValueError("Browser locator snapshot token cannot be empty")
        result = self._submit(
            "extract_snapshot_locator_text",
            args=(token, index, child_selector),
            timeout=timeout,
        )
        if not isinstance(result, str):
            raise TypeError("Browser snapshot text extraction returned a non-text result")
        return result

    def count_locators(
        self,
        locator: object,
        *,
        scope: object = None,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> int:
        """Count matches below the page or an optional container on the owner thread."""
        result = self._submit(
            "count_locators",
            args=(LocatorSpec.parse(locator), BrowserDomScope.parse(scope)),
            timeout=timeout,
        )
        if not isinstance(result, int) or isinstance(result, bool) or result < 0:
            raise TypeError("Browser locator count returned an invalid result")
        return result

    def snapshot_locators(
        self,
        locator: object,
        *,
        scope: object = None,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> ElementCollectionSnapshot:
        result = self._submit(
            "snapshot_locators",
            args=(LocatorSpec.parse(locator), BrowserDomScope.parse(scope)),
            timeout=timeout,
        )
        if not isinstance(result, ElementCollectionSnapshot):
            raise TypeError("Browser locator snapshot returned an invalid result")
        return result

    def click_snapshot_locator(
        self,
        token: str,
        index: int,
        *,
        timeout_ms: int | None = None,
        anchor: str = "center",
        offset_x: float = 0,
        offset_y: float = 0,
        offset_unit: str = "pixels",
        require_new_child_tab: bool = False,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise ValueError("Browser locator snapshot token cannot be empty")
        self._submit(
            "click_snapshot_locator",
            args=(
                token,
                index,
                timeout_ms,
                anchor,
                offset_x,
                offset_y,
                offset_unit,
                require_new_child_tab,
            ),
            timeout=timeout,
        )

    def release_locator_snapshot(
        self,
        token: str,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        if token:
            self._submit("release_locator_snapshot", args=(token,), timeout=timeout)

    def start_element_picker(
        self,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._submit("start_element_picker", timeout=timeout)

    def poll_element_picker(
        self,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> PickedElement | None:
        result = self._submit("poll_element_picker", timeout=timeout)
        if result is not None and not isinstance(result, PickedElement):
            raise TypeError("Browser element picker returned an invalid result")
        return result

    def stop_element_picker(
        self,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> PickedElement | None:
        result = self._submit("stop_element_picker", timeout=timeout)
        if result is not None and not isinstance(result, PickedElement):
            raise TypeError("Browser element picker returned an invalid result")
        return result

    def pick_element(
        self,
        *,
        timeout: float = _DEFAULT_OPERATION_TIMEOUT_SECONDS,
        poll_interval: float = 0.05,
    ) -> PickedElement:
        """Wait for one user pick; intended for a UI background worker."""
        if poll_interval <= 0:
            raise ValueError("Picker poll interval must be positive")
        deadline = time.monotonic() + timeout
        self.start_element_picker(timeout=timeout)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for a browser element pick")
                result = self.poll_element_picker(timeout=remaining)
                if result is not None:
                    return result
                time.sleep(min(poll_interval, remaining))
        finally:
            remaining = max(0.1, deadline - time.monotonic())
            self.stop_element_picker(timeout=remaining)

    def execute_flow(
        self,
        executor: Any,
        flow: Any,
        ctx: Any,
        *,
        use: ProfileUse,
        borrowed_lease: BorrowedProfileLease | None = None,
        timeout: float | None = None,
    ) -> None:
        """Execute a flow against this exact live browser on its owner thread."""
        self._submit_borrowed(
            "execute_flow",
            (executor, flow, ctx),
            use=use,
            borrowed_lease=borrowed_lease,
            timeout=timeout,
        )

    def execute_command(
        self,
        executor: Any,
        command: Any,
        ctx: Any,
        *,
        use: ProfileUse,
        borrowed_lease: BorrowedProfileLease | None = None,
        timeout: float | None = None,
    ) -> None:
        """Execute one command against this exact live browser owner."""
        self._submit_borrowed(
            "execute_command",
            (executor, command, ctx),
            use=use,
            borrowed_lease=borrowed_lease,
            timeout=timeout,
        )

    def close(
        self,
        *,
        timeout: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        """Close Playwright resources on their owner thread.

        This method is idempotent.  Completion means the owner has run browser
        teardown, not merely that a close request was queued.
        """
        if self._closed.is_set():
            return
        if self._profile_lease is not None:
            self._profile_lease.begin_close()
        close_future = self._request_close()
        close_future.result(timeout=timeout)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise TimeoutError("Browser session owner did not stop after close")

    def _submit(
        self,
        operation: Literal[
            "screenshot",
            "tabs_snapshot",
            "select_tab",
            "click_locator",
            "wait_for_locator",
            "fill_locator",
            "extract_locator_text",
            "extract_snapshot_locator_text",
            "assert_locator_visible",
            "is_locator_visible",
            "inspect_locator",
            "count_locators",
            "snapshot_locators",
            "click_snapshot_locator",
            "release_locator_snapshot",
            "start_element_picker",
            "poll_element_picker",
            "stop_element_picker",
        ],
        *,
        args: tuple[Any, ...] = (),
        timeout: float,
    ) -> Any:
        future: Future[Any] = Future()
        with self._state_lock:
            if self._closing or self._closed.is_set():
                raise RuntimeError("Browser session is closing or already closed")
            self._requests.put(_BrowserRequest(operation, future, args=args))
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            # A request that has not started can be removed logically from the
            # FIFO.  In-flight Playwright calls cannot be interrupted safely,
            # but queued stale captures must not delay a later close.
            if not future.done() and future.cancel():
                raise
            if future.done():
                return future.result()
            if operation == "select_tab":
                raise BrowserSessionOperationUncertainError(
                    "Browser tab selection started but did not finish before its "
                    "deadline. Refresh the tab list before choosing another tab."
                )
            if operation in {
                "click_locator",
                "wait_for_locator",
                "fill_locator",
                "start_element_picker",
                "stop_element_picker",
            }:
                raise BrowserSessionOperationUncertainError(
                    f"Browser operation {operation!r} started but did not finish "
                    "before its deadline."
                )
            raise

    def _submit_borrowed(
        self,
        operation: Literal["execute_flow", "execute_command"],
        args: tuple[Any, ...],
        *,
        use: ProfileUse,
        borrowed_lease: BorrowedProfileLease | None,
        timeout: float | None,
    ) -> Any:
        release_borrow = borrowed_lease is None
        if borrowed_lease is not None:
            if self._profile_lease is None:
                raise ValueError(
                    "A borrowed profile lease cannot be used with a temporary "
                    "browser session."
                )
            if borrowed_lease.use != use:
                raise ValueError(
                    "The borrowed profile lease belongs to a different operation."
                )
            borrowed_lease.ensure_live(self._profile_lease)
            borrow = borrowed_lease
        else:
            borrow = (
                self._profile_lease.borrow(use)
                if self._profile_lease is not None
                else None
            )
        future: Future[Any] = Future()
        try:
            with self._state_lock:
                if self._closing or self._closed.is_set():
                    raise RuntimeError(
                        "Browser session is closing or already closed"
                    )
                self._requests.put(
                    _BrowserRequest(
                        operation,
                        future,
                        args=args,
                        borrow=borrow,
                        release_borrow=release_borrow,
                    )
                )
        except BaseException:
            if borrow is not None and release_borrow:
                borrow.release()
            raise
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            # If queued, cancellation prevents execution and the owner releases
            # the borrow when it dequeues the request.  If already running, the
            # borrow remains held until owner-thread execution safely finishes.
            if not future.done():
                future.cancel()
            raise

    def _request_close(self) -> Future[None]:
        with self._state_lock:
            if not self._closing and not self._closed.is_set():
                self._closing = True
                self._requests.put(_BrowserRequest("close", self._close_future))
        return self._close_future

    def _run_owner(self) -> None:
        session_data: BrowserSessionData | CompanionBrowserPort | None = None
        close_error: BaseException | None = None
        teardown_confirmed = True
        try:
            session_data = self._engine.connect(
                self._device,
                profile_lease=self._profile_lease,
            )
            companion = isinstance(session_data, CompanionBrowserPort)
            self._startup.set_result(None)

            while True:
                request = self._requests.get()
                if request.operation == "close":
                    break
                if not request.future.set_running_or_notify_cancel():
                    if request.borrow is not None and request.release_borrow:
                        request.borrow.release()
                    continue
                try:
                    if request.operation == "screenshot":
                        request.future.set_result(session_data.screenshot())
                    elif request.operation == "tabs_snapshot":
                        request.future.set_result(session_data.tabs_snapshot())
                    elif request.operation == "select_tab":
                        session_data.select_tab(str(request.args[0]))
                        request.future.set_result(None)
                    elif request.operation == "click_locator":
                        locator, timeout_ms, anchor, offset_x, offset_y, offset_unit, require_new_child_tab = request.args
                        if companion:
                            session_data.click_locator(
                                locator, timeout_ms=timeout_ms, anchor=anchor,
                                offset_x=offset_x, offset_y=offset_y,
                                offset_unit=offset_unit,
                                require_new_child_tab=require_new_child_tab,
                            )
                        else:
                            click_element(
                                session_data, locator, timeout_ms=timeout_ms,
                                anchor=anchor, offset_x=offset_x,
                                offset_y=offset_y, offset_unit=offset_unit,
                                require_new_child_tab=require_new_child_tab,
                            )
                        request.future.set_result(None)
                    elif request.operation == "wait_for_locator":
                        locator, timeout_ms = request.args
                        if companion:
                            session_data.wait_for_locator(locator, timeout_ms=timeout_ms)
                        else:
                            wait_for_element(session_data, locator, timeout_ms=timeout_ms)
                        request.future.set_result(None)
                    elif request.operation == "fill_locator":
                        locator, value, min_delay, max_delay = request.args
                        if companion:
                            session_data.fill_locator(
                                locator, value, min_delay=min_delay,
                                max_delay=max_delay,
                            )
                        else:
                            fill_element(session_data, locator, value, min_delay, max_delay)
                        request.future.set_result(None)
                    elif request.operation == "extract_locator_text":
                        request.future.set_result(
                            session_data.extract_locator_text(request.args[0])
                            if companion else
                            extract_element_text(session_data, request.args[0])
                        )
                    elif request.operation == "extract_snapshot_locator_text":
                        token, index, child_selector = request.args
                        request.future.set_result(
                            session_data.extract_snapshot_locator_text(
                                token, index, child_selector=child_selector,
                            ) if companion else extract_snapshot_element_text(
                                session_data, token, index,
                                child_selector=child_selector,
                            )
                        )
                    elif request.operation == "assert_locator_visible":
                        if companion:
                            session_data.assert_locator_visible(request.args[0])
                        else:
                            assert_element_visible(session_data, request.args[0])
                        request.future.set_result(None)
                    elif request.operation == "is_locator_visible":
                        request.future.set_result(
                            session_data.is_locator_visible(request.args[0])
                            if companion else
                            is_element_visible(session_data, request.args[0])
                        )
                    elif request.operation == "scroll_page":
                        delta_x, delta_y = request.args
                        if companion:
                            session_data.scroll_page(delta_x=delta_x, delta_y=delta_y)
                        else:
                            scroll_page(session_data, delta_x=delta_x, delta_y=delta_y)
                        request.future.set_result(None)
                    elif request.operation == "scroll_locator_into_view":
                        locator, timeout_ms = request.args
                        if companion:
                            session_data.scroll_locator_into_view(
                                locator, timeout_ms=timeout_ms
                            )
                        else:
                            scroll_element_into_view(
                                session_data, locator, timeout_ms=timeout_ms
                            )
                        request.future.set_result(None)
                    elif request.operation == "scroll_snapshot_locator_into_view":
                        token, index, timeout_ms = request.args
                        if companion:
                            session_data.scroll_snapshot_locator_into_view(
                                token, index, timeout_ms=timeout_ms
                            )
                        else:
                            scroll_snapshot_element_into_view(
                                session_data, token, index, timeout_ms=timeout_ms
                            )
                        request.future.set_result(None)
                    elif request.operation == "inspect_locator":
                        request.future.set_result(
                            inspect_element(session_data, request.args[0])
                        )
                    elif request.operation == "count_locators":
                        locator, scope = request.args
                        request.future.set_result(
                            session_data.count_locators(locator, scope=scope)
                            if companion else
                            count_elements(session_data, locator, scope=scope)
                        )
                    elif request.operation == "snapshot_locators":
                        locator, scope = request.args
                        request.future.set_result(
                            session_data.snapshot_locators(locator, scope=scope)
                            if companion else
                            snapshot_elements(session_data, locator, scope=scope)
                        )
                    elif request.operation == "click_snapshot_locator":
                        token, index, timeout_ms, anchor, offset_x, offset_y, offset_unit, require_new_child_tab = request.args
                        if companion:
                            session_data.click_snapshot_locator(
                                token, index, timeout_ms=timeout_ms,
                                anchor=anchor, offset_x=offset_x,
                                offset_y=offset_y, offset_unit=offset_unit,
                                require_new_child_tab=require_new_child_tab,
                            )
                        else:
                            click_snapshot_element(
                                session_data, token, index,
                                timeout_ms=timeout_ms, anchor=anchor,
                                offset_x=offset_x, offset_y=offset_y,
                                offset_unit=offset_unit,
                                require_new_child_tab=require_new_child_tab,
                            )
                        request.future.set_result(None)
                    elif request.operation == "release_locator_snapshot":
                        if companion:
                            session_data.release_locator_snapshot(request.args[0])
                        else:
                            release_element_snapshot(session_data, request.args[0])
                        request.future.set_result(None)
                    elif request.operation == "start_element_picker":
                        start_element_picker(session_data)
                        request.future.set_result(None)
                    elif request.operation == "poll_element_picker":
                        request.future.set_result(poll_element_picker(session_data))
                    elif request.operation == "stop_element_picker":
                        request.future.set_result(stop_element_picker(session_data))
                    elif request.operation in ("execute_flow", "execute_command"):
                        if (
                            request.borrow is not None
                            and not request.release_borrow
                            and self._profile_lease is not None
                        ):
                            request.borrow.ensure_live(self._profile_lease)
                        executor, executable, ctx = request.args
                        original_session = ctx.session
                        ctx.session = DeviceSession(
                            device=self._device,
                            data={"browser_data": session_data},
                        )
                        try:
                            if request.operation == "execute_flow":
                                executor.execute(executable, ctx)
                            else:
                                executor.execute_command(
                                    executable,
                                    ctx,
                                    flow_name="Interactive command",
                                )
                        finally:
                            ctx.session = original_session
                        request.future.set_result(None)
                except BaseException as exc:
                    request.future.set_exception(exc)
                finally:
                    if request.borrow is not None and request.release_borrow:
                        request.borrow.release()
        except BaseException as exc:
            if isinstance(exc, BrowserSessionTeardownError):
                teardown_confirmed = False
            if not self._startup.done():
                self._startup.set_exception(exc)
            else:
                logger.exception("Interactive browser session owner failed")
                close_error = exc
        finally:
            if session_data is not None:
                try:
                    self._engine.disconnect(session_data)
                except BaseException as exc:
                    logger.exception("Interactive browser session teardown failed")
                    close_error = close_error or exc
                    teardown_confirmed = False
            if self._profile_lease is not None and teardown_confirmed:
                try:
                    self._profile_lease.release()
                except BaseException as exc:
                    logger.exception("Browser profile owner release failed")
                    close_error = close_error or exc
            elif self._profile_lease is not None:
                logger.error(
                    "Retaining browser profile lease %s because teardown was "
                    "not confirmed; the profile is quarantined until restart",
                    self._profile_lease.profile_id,
                )

            if not self._close_future.done():
                if close_error is None:
                    self._close_future.set_result(None)
                else:
                    self._close_future.set_exception(close_error)
            self._closed.set()
