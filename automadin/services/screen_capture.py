"""Validated, in-memory screenshots for design and calibration tools.

This module deliberately has no Qt dependency.  Connected-target capture remains
behind :class:`DeviceSessionService`, which preserves Playwright owner-thread
rules and the Android/Windows engine boundary.  File import and provider output
share one validation path and return canonical PNG bytes.
"""

from __future__ import annotations

import io
import logging
import os
import stat
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    DeviceSession,
    WindowsDevice,
)
from automadin.services.image_files import normalize_image_bytes_to_png

logger = logging.getLogger(__name__)

DEFAULT_MAX_CAPTURE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_CAPTURE_PIXELS = 12_000_000
DEFAULT_MAX_DECODED_BYTES = 48 * 1024 * 1024
DEFAULT_MAX_CAPTURE_DIMENSION = 16_384
_WINDOWS_REPARSE_POINT_ATTRIBUTE = 0x400
_WINDOWS_REMOTE_DRIVE = 4


class CaptureError(RuntimeError):
    """Base class for user-readable capture failures."""


class CaptureSourceUnavailableError(CaptureError):
    """The requested session or file is no longer available."""


class CaptureSourceChangedError(CaptureError):
    """The requested source changed while it was being captured."""


class InvalidCaptureImageError(CaptureError):
    """Provider or file bytes are not a supported image."""


class CaptureImageTooLargeError(CaptureError):
    """Encoded or decoded image bounds exceed the design-tool budget."""


class CaptureProviderError(CaptureError):
    """A connected provider failed without exposing its raw diagnostics."""


class CaptureSourceKind(str, Enum):
    BROWSER = "browser"
    ANDROID = "android"
    WINDOWS = "windows"
    FILE = "file"


class CaptureCoordinateSpace(str, Enum):
    """The pixel coordinate system represented by one frame."""

    BROWSER_VIEWPORT = "browser_viewport"
    ANDROID_DISPLAY = "android_display"
    WINDOWS_WINDOW = "windows_window"
    OFFLINE_IMAGE = "offline_image"


@dataclass(frozen=True, slots=True)
class CaptureSource:
    """Stable, non-sensitive identity for a captured frame."""

    kind: CaptureSourceKind
    identifier: str
    label: str
    coordinate_space: CaptureCoordinateSpace
    session_id: str | None = None
    session_token: int | None = None


@dataclass(frozen=True, slots=True)
class CaptureFrame:
    """A validated screenshot held only in memory until a user commits it."""

    png_bytes: bytes
    width: int
    height: int
    source: CaptureSource


class ConnectedCapturePort(Protocol):
    """Minimal connected-session boundary required by the capture service."""

    def get_session(self, session_id: str) -> DeviceSession | None: ...

    def take_screenshot(self, session: DeviceSession) -> bytes: ...


class ScreenCaptureService:
    """Capture connected targets or import files without mutating app state."""

    def __init__(
        self,
        session_service: ConnectedCapturePort | None,
        *,
        max_encoded_bytes: int = DEFAULT_MAX_CAPTURE_BYTES,
        max_pixels: int = DEFAULT_MAX_CAPTURE_PIXELS,
        max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES,
        max_dimension: int = DEFAULT_MAX_CAPTURE_DIMENSION,
    ) -> None:
        if max_encoded_bytes <= 0:
            raise ValueError("max_encoded_bytes must be positive")
        if max_pixels <= 0:
            raise ValueError("max_pixels must be positive")
        if max_decoded_bytes <= 0:
            raise ValueError("max_decoded_bytes must be positive")
        if max_dimension <= 0:
            raise ValueError("max_dimension must be positive")
        self._session_service = session_service
        self._max_encoded_bytes = int(max_encoded_bytes)
        self._max_pixels = int(max_pixels)
        self._max_decoded_bytes = int(max_decoded_bytes)
        self._max_dimension = int(max_dimension)

    def capture_session(self, session: DeviceSession) -> CaptureFrame:
        """Capture one still frame from an exact active session.

        The identity is checked before and after the provider call.  A result is
        therefore rejected if the target disconnects, is replaced, or changes
        identity while capture is in flight.
        """
        if self._session_service is None:
            raise CaptureSourceUnavailableError(
                "Connected-target capture is not available."
            )
        source = self._source_for_session(session)
        self._assert_active_source(session, source)
        try:
            payload = self._session_service.take_screenshot(session)
        except Exception as exc:
            logger.warning(
                "Connected screenshot provider failed: source=%s exception=%s",
                source.kind.value,
                type(exc).__name__,
            )
            raise CaptureProviderError(
                "The connected target could not be captured. Check its "
                "connection and try again."
            ) from exc
        self._assert_active_source(session, source)
        frame = self._frame_from_bytes(payload, source)
        self._assert_active_source(session, source)
        return frame

    def validate_frame_source(self, frame: CaptureFrame) -> None:
        """Reject a connected frame whose exact source is no longer active."""
        source = frame.source
        if source.session_id is None:
            return
        if self._session_service is None:
            raise CaptureSourceUnavailableError(
                "The selected target is no longer connected."
            )
        active = self._session_service.get_session(source.session_id)
        if active is None or not capture_source_matches_session(source, active):
            raise CaptureSourceChangedError(
                "The selected target changed before the screenshot could be shown. "
                "Try again."
            )

    def import_file(self, path: str | Path) -> CaptureFrame:
        """Read a bounded regular image file and return canonical PNG bytes."""
        source_path = Path(os.path.abspath(os.fspath(path)))
        if self._is_remote_file_source(source_path):
            raise CaptureSourceUnavailableError(
                "Copy the selected image to a local drive before importing it."
            )
        label = source_path.name.strip() or "Imported image"
        source = CaptureSource(
            kind=CaptureSourceKind.FILE,
            identifier=label,
            label=label,
            coordinate_space=CaptureCoordinateSpace.OFFLINE_IMAGE,
        )
        before = self._lstat_regular_file(source_path)
        if before.st_size > self._max_encoded_bytes:
            raise CaptureImageTooLargeError(
                "The selected image is too large to import safely."
            )

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(source_path, flags)
            opened_before = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or self._file_identity(before) != self._file_identity(opened_before)
            ):
                raise CaptureSourceChangedError(
                    "The selected image changed before it could be imported. "
                    "Try again."
                )
            with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                payload = stream.read(self._max_encoded_bytes + 1)
            opened_after = os.fstat(file_descriptor)
            after = self._lstat_regular_file(source_path)
        except CaptureError:
            raise
        except OSError as exc:
            raise CaptureSourceUnavailableError(
                "The selected image could not be read."
            ) from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
        if len(payload) > self._max_encoded_bytes:
            raise CaptureImageTooLargeError(
                "The selected image is too large to import safely."
            )
        identities = {
            self._file_identity(before),
            self._file_identity(opened_before),
            self._file_identity(opened_after),
            self._file_identity(after),
        }
        if len(identities) != 1:
            raise CaptureSourceChangedError(
                "The selected image changed while it was being imported. Try again."
            )
        return self._frame_from_bytes(payload, source)

    @classmethod
    def _lstat_regular_file(cls, path: Path) -> os.stat_result:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise CaptureSourceUnavailableError(
                "The selected image file is no longer available."
            ) from exc
        is_reparse_point = bool(
            getattr(metadata, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or is_reparse_point
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise CaptureSourceUnavailableError(
                "The selected image must be a direct regular file."
            )
        return metadata

    @staticmethod
    def _is_remote_file_source(path: Path) -> bool:
        """Reject network paths whose I/O cannot be bounded by this process."""
        if os.name != "nt":
            return False
        drive = path.drive
        if drive.startswith("\\\\"):
            return True
        if not drive:
            return False
        try:
            import ctypes

            drive_type = ctypes.windll.kernel32.GetDriveTypeW(  # type: ignore[attr-defined]
                f"{drive}\\"
            )
        except (AttributeError, OSError):
            return False
        return int(drive_type) == _WINDOWS_REMOTE_DRIVE

    @staticmethod
    def _file_identity(metadata: object) -> tuple[object, ...]:
        # On modern Windows, ``st_ctime_ns`` is metadata-change time and can
        # differ between lstat() and fstat() for the same open file. Creation
        # time is exposed separately and remains stable across those views.
        creation_or_change_time = (
            getattr(metadata, "st_birthtime_ns", None)
            if os.name == "nt"
            else getattr(metadata, "st_ctime_ns", None)
        )
        return (
            getattr(metadata, "st_dev", None),
            getattr(metadata, "st_ino", None),
            getattr(metadata, "st_size", None),
            getattr(metadata, "st_mtime_ns", None),
            creation_or_change_time,
        )

    def _frame_from_bytes(
        self,
        payload: object,
        source: CaptureSource,
    ) -> CaptureFrame:
        if not isinstance(payload, bytes) or not payload:
            raise InvalidCaptureImageError(
                "The capture source returned an empty or invalid image."
            )
        if len(payload) > self._max_encoded_bytes:
            raise CaptureImageTooLargeError(
                "The captured image is too large to process safely."
            )

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    width, height = image.size
            if width <= 0 or height <= 0:
                raise InvalidCaptureImageError(
                    "The capture source returned an image with invalid dimensions."
                )
            if width > self._max_dimension or height > self._max_dimension:
                raise CaptureImageTooLargeError(
                    "The captured image dimensions are too large to process safely."
                )
            if width * height > self._max_pixels:
                raise CaptureImageTooLargeError(
                    "The captured image has too many pixels to process safely."
                )
            if width * height * 4 > self._max_decoded_bytes:
                raise CaptureImageTooLargeError(
                    "The captured image needs too much decoded memory to process "
                    "safely."
                )
            png_bytes, normalized_width, normalized_height = (
                normalize_image_bytes_to_png(payload, optimize=False)
            )
        except CaptureError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as exc:
            raise InvalidCaptureImageError(
                "The capture source did not provide a supported image."
            ) from exc

        if len(png_bytes) > self._max_encoded_bytes:
            raise CaptureImageTooLargeError(
                "The normalized image is too large to process safely."
            )
        return CaptureFrame(
            png_bytes=png_bytes,
            width=normalized_width,
            height=normalized_height,
            source=source,
        )

    def _assert_active_source(
        self,
        session: DeviceSession,
        expected: CaptureSource,
    ) -> None:
        if self._session_service is None:
            raise CaptureSourceUnavailableError(
                "Connected-target capture is not available."
            )
        active = self._session_service.get_session(session.session_id)
        if active is not session:
            raise CaptureSourceUnavailableError(
                "The selected target is no longer connected."
            )
        current = self._source_for_session(active)
        if current != expected:
            raise CaptureSourceChangedError(
                "The selected target changed during capture. Try again."
            )

    @staticmethod
    def _source_for_session(session: DeviceSession) -> CaptureSource:
        device = session.device
        if isinstance(device, BrowserDevice):
            kind = CaptureSourceKind.BROWSER
            coordinate_space = CaptureCoordinateSpace.BROWSER_VIEWPORT
        elif isinstance(device, AndroidDevice):
            kind = CaptureSourceKind.ANDROID
            coordinate_space = CaptureCoordinateSpace.ANDROID_DISPLAY
        elif isinstance(device, WindowsDevice):
            kind = CaptureSourceKind.WINDOWS
            coordinate_space = CaptureCoordinateSpace.WINDOWS_WINDOW
        else:
            raise CaptureSourceUnavailableError(
                "The selected target does not support screenshots."
            )
        return CaptureSource(
            kind=kind,
            identifier=str(device.id),
            label=str(device.name),
            coordinate_space=coordinate_space,
            session_id=session.session_id,
            session_token=id(session),
        )


def capture_source_matches_session(
    source: CaptureSource,
    session: DeviceSession,
) -> bool:
    """Return whether ``session`` is the exact live source captured in ``source``."""
    if (
        source.session_id != session.session_id
        or source.session_token != id(session)
    ):
        return False
    try:
        current = ScreenCaptureService._source_for_session(session)
    except CaptureError:
        return False
    return current == source
