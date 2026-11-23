from __future__ import annotations

from types import SimpleNamespace

from automadin.windows_capture_worker import capture_window_png


class _Bitmap:
    def CreateCompatibleBitmap(self, _dc, _width: int, _height: int) -> None:
        return None

    def GetInfo(self) -> dict[str, int]:
        return {"bmWidth": 1, "bmHeight": 1}

    def GetBitmapBits(self, _signed: bool) -> bytes:
        return b"\x00\x00\x00\x00"

    def GetHandle(self) -> int:
        return 3


class _Dc:
    def CreateCompatibleDC(self):
        return self

    def SelectObject(self, bitmap):
        return bitmap

    def GetSafeHdc(self) -> int:
        return 2

    def DeleteDC(self) -> None:
        return None


class _GraphicsCapture:
    client_size = (1, 1)

    def __init__(self) -> None:
        self.started = False

    def start_capture(self) -> None:
        self.started = True

    def next(self) -> bytes:
        assert self.started
        return b"\x00\x00\xff\xff"


def test_capture_uses_occlusion_safe_backend_when_print_window_is_rejected() -> None:
    dc = _Dc()
    bitmap = _Bitmap()
    graphics_capture = _GraphicsCapture()
    user32 = SimpleNamespace(
        IsWindow=lambda _hwnd: True,
        PrintWindow=lambda *_args: False,
    )
    win32gui = SimpleNamespace(
        GetWindowRect=lambda _hwnd: (0, 0, 1, 1),
        GetWindowDC=lambda _hwnd: 1,
        GetWindowText=lambda _hwnd: "Game",
        GetClassName=lambda _hwnd: "GameWindow",
        ClientToScreen=lambda _hwnd, _point: (0, 0),
        DeleteObject=lambda _handle: None,
        ReleaseDC=lambda _hwnd, _dc: None,
    )
    win32ui = SimpleNamespace(
        CreateDCFromHandle=lambda _handle: dc,
        CreateBitmap=lambda: bitmap,
    )

    screenshot = capture_window_png(
        10,
        user32=user32,
        win32gui=win32gui,
        win32ui=win32ui,
        capture_factory=lambda title, class_name: (
            graphics_capture
            if (title, class_name) == ("Game", "GameWindow")
            else None
        ),
    )

    assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
