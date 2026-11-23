"""Native Windows capture entry point used by the bounded supervisor."""

from __future__ import annotations

import ctypes
import io
import os
import sys
from pathlib import Path
from typing import Any, Callable

_MAX_CAPTURE_DIMENSION = 16_384
_MAX_CAPTURE_PIXELS = 12_000_000
_PW_RENDERFULLCONTENT = 2

CaptureFactory = Callable[[str, str], Any]


def _windows_graphics_capture_factory(title: str, class_name: str) -> Any:
    from windows_cap import WindowCapture  # type: ignore[import-not-found]

    return WindowCapture(title, class_name)


def _capture_with_windows_graphics(
    hwnd: int,
    width: int,
    height: int,
    *,
    win32gui: Any,
    capture_factory: CaptureFactory,
) -> bytes:
    """Capture an occlusion-safe client frame and preserve window coordinates."""
    from PIL import Image

    title = str(win32gui.GetWindowText(hwnd))
    class_name = str(win32gui.GetClassName(hwnd))
    if not title:
        raise RuntimeError("The target window has no title for graphics capture.")

    capture = capture_factory(title, class_name)
    capture.start_capture()
    payload = capture.next()
    client_width, client_height = map(int, capture.client_size)
    expected_size = client_width * client_height * 4
    if (
        client_width <= 0
        or client_height <= 0
        or not isinstance(payload, bytes)
        or len(payload) != expected_size
    ):
        raise RuntimeError("Windows Graphics Capture returned an invalid frame.")

    client_left, client_top = map(int, win32gui.ClientToScreen(hwnd, (0, 0)))
    window_left, window_top, _right, _bottom = win32gui.GetWindowRect(hwnd)
    offset_x = client_left - int(window_left)
    offset_y = client_top - int(window_top)
    if (
        offset_x < 0
        or offset_y < 0
        or offset_x + client_width > width
        or offset_y + client_height > height
    ):
        raise RuntimeError(
            "Windows Graphics Capture client bounds do not fit the target window."
        )

    client_image = Image.frombuffer(
        "RGBA",
        (client_width, client_height),
        payload,
        "raw",
        "BGRA",
        0,
        1,
    ).convert("RGB")
    image = Image.new("RGB", (width, height))
    image.paste(client_image, (offset_x, offset_y))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def capture_window_png(
    hwnd: int,
    *,
    user32: Any | None = None,
    win32gui: Any | None = None,
    win32ui: Any | None = None,
    capture_factory: CaptureFactory | None = None,
) -> bytes:
    """Capture one live window without including overlapping windows."""
    if sys.platform != "win32":
        raise RuntimeError("Windows capture is available only on Windows.")

    if user32 is None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    if win32gui is None:
        import win32gui as imported_win32gui  # type: ignore

        win32gui = imported_win32gui
    if win32ui is None:
        import win32ui as imported_win32ui  # type: ignore

        win32ui = imported_win32ui
    from PIL import Image

    if not user32.IsWindow(hwnd):
        raise RuntimeError("Window no longer exists.")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("Window has invalid capture dimensions.")
    if (
        width > _MAX_CAPTURE_DIMENSION
        or height > _MAX_CAPTURE_DIMENSION
        or width * height > _MAX_CAPTURE_PIXELS
    ):
        raise RuntimeError("Window is too large to capture safely.")

    hwnd_dc = 0
    mfc_dc = None
    save_dc = None
    bitmap = None
    previous_bitmap = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            raise RuntimeError("Could not access the window display.")
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        previous_bitmap = save_dc.SelectObject(bitmap)

        rendered = user32.PrintWindow(
            hwnd,
            save_dc.GetSafeHdc(),
            _PW_RENDERFULLCONTENT,
        )
        if not rendered:
            # BitBlt on a game window DC can copy whatever is visibly covering
            # the target. Windows Graphics Capture reads the target's composed
            # client surface instead, including while another window overlaps.
            return _capture_with_windows_graphics(
                hwnd,
                width,
                height,
                win32gui=win32gui,
                capture_factory=(
                    capture_factory or _windows_graphics_capture_factory
                ),
            )
        bitmap_info = bitmap.GetInfo()
        bitmap_data = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (bitmap_info["bmWidth"], bitmap_info["bmHeight"]),
            bitmap_data,
            "raw",
            "BGRX",
            0,
            1,
        )
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        if save_dc is not None and previous_bitmap is not None:
            save_dc.SelectObject(previous_bitmap)
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if save_dc is not None:
            save_dc.DeleteDC()
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        if hwnd_dc:
            win32gui.ReleaseDC(hwnd, hwnd_dc)


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main(arguments: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    if len(values) != 2:
        return 2
    try:
        hwnd = int(values[0])
        output_path = Path(values[1])
        if hwnd <= 0 or output_path.exists() or not output_path.parent.is_dir():
            return 2
        _write_exclusive(output_path, capture_window_png(hwnd))
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
