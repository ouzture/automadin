from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from automadin.execution.context import FlowContext
from automadin.execution.image_matching import find_stored_image
from automadin.models.device import (
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
)
from automadin.models.resolution_scaling import (
    ResolutionScalingMode,
    ResolutionScalingSettings,
)
from automadin.services.find_image import FindImageService, cv2


def _png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return bytes(encoded)


def test_stored_image_capture_surface_overrides_flow_fallback_baseline() -> None:
    rng = np.random.default_rng(19)
    template = rng.integers(0, 256, size=(12, 16, 3), dtype=np.uint8)
    resized = cv2.resize(template, (32, 24), interpolation=cv2.INTER_LINEAR)
    screenshot = np.zeros((200, 200, 3), dtype=np.uint8)
    screenshot[70:94, 80:112] = resized
    asset = SimpleNamespace(
        data=_png(template),
        source_surface_size=(100, 100),
        source_coordinate_space="browser_viewport",
    )
    database = SimpleNamespace(load_image_asset=lambda _name: asset)
    session = DeviceSession(
        device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM))
    )
    ctx = FlowContext(session)
    settings = ResolutionScalingSettings(
        ResolutionScalingMode.ADAPTIVE,
        1000,
        1000,
    )

    with ctx.bind_resolution_scaling(settings):
        exists, result = find_stored_image(
            ctx,
            database,
            FindImageService(),
            _png(screenshot),
            "button",
            (200, 200),
            threshold=0.99,
            color_match=False,
            color_tolerance=0.3,
        )

    assert exists
    assert result is not None
    assert (result.x, result.y, result.width, result.height) == (80, 70, 32, 24)
