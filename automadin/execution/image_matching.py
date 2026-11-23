"""Resolution-aware matching for templates stored in the workspace database."""

from __future__ import annotations

from typing import Any

from automadin.execution.context import FlowContext
from automadin.models.device import AndroidDevice, BrowserDevice, WindowsDevice
from automadin.services.find_image import FindImageService, MatchResult


def format_image_match(
    image_name: str,
    result: MatchResult,
    *,
    threshold: float,
    color_match: bool,
    color_tolerance: float,
) -> str:
    """Format one consistent, user-facing image-match diagnostic."""
    color_detail = "color check=off"
    if color_match:
        distance = result.color_distance
        color_detail = (
            f"color distance={distance:.4f} <= tolerance={color_tolerance:.4f}"
            if distance is not None
            else f"color check=passed; tolerance={color_tolerance:.4f}"
        )
    return (
        f"Image match: '{image_name}'; confidence={result.confidence:.4f} "
        f">= threshold={threshold:.4f}; {color_detail}; "
        f"bounds=({result.x}, {result.y}, {result.width}x{result.height}); "
        f"center=({result.center_x}, {result.center_y})"
    )


def find_stored_image(
    ctx: FlowContext,
    database: Any,
    service: FindImageService,
    screenshot: bytes,
    image_name: str,
    surface_size: tuple[int, int],
    *,
    threshold: float,
    color_match: bool,
    color_tolerance: float,
) -> tuple[bool, MatchResult | None]:
    """Load and match one image, returning whether the asset existed."""
    asset_loader = getattr(database, "load_image_asset", None)
    asset = asset_loader(image_name) if callable(asset_loader) else None
    if asset is not None:
        template = asset.data
        source_spaces = {
            BrowserDevice: "browser_viewport",
            AndroidDevice: "android_display",
            WindowsDevice: "windows_window",
        }
        current_space = next(
            (
                value
                for device_type, value in source_spaces.items()
                if isinstance(ctx.session.device, device_type)
            ),
            "",
        )
        reference_size = (
            asset.source_surface_size
            if not asset.source_coordinate_space
            or asset.source_coordinate_space == current_space
            else None
        )
    else:
        template = database.load_image(image_name)
        reference_size = None
    if template is None:
        return False, None

    if ctx.resolution_scaling_active:
        transform = ctx.resolution_transform(
            surface_size,
            reference_size=reference_size,
        )
        return True, service.find_scaled(
            screenshot,
            template,
            transform.template_scales(),
            threshold,
            color_match,
            color_tolerance,
        )
    return True, service.find(
        screenshot,
        template,
        threshold,
        color_match,
        color_tolerance,
    )


__all__ = ["find_stored_image", "format_image_match"]
