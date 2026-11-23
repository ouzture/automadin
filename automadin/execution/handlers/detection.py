from __future__ import annotations

from io import BytesIO
import logging

from automadin.engine.device_controller import (
    ScreenshotCapability,
    get_device_capability,
)
from automadin.engine.windows_engine import WindowsDeviceEngine
from automadin.execution.context import FlowContext
from automadin.execution.image_matching import find_stored_image, format_image_match
from automadin.execution.attention import AttentionResolution, resolve_not_found_policy
from automadin.execution.runtime_diagnostics import (
    format_runtime_value,
    variable_display_name,
)
from automadin.models.device import BrowserDevice, WindowsDevice
from automadin.models.flow import FlowCommand
from automadin.models.hook import HookPhase, HookType
from automadin.models.typed_value import ValueType
from automadin.services.find_image import FindImageService
from automadin.services.image_search_region import (
    ImageSearchRegionError,
    prepare_search_region,
    to_absolute_match,
)
from automadin.services.ocr import ScreenTextOcrEngine
from automadin.services.screen_text_reading import (
    ScreenTextReadingError,
    read_screen_text,
)

logger = logging.getLogger(__name__)

_find_image_service = FindImageService()


def _resolve_image_names(cmd: FlowCommand, ctx: FlowContext) -> list[str]:
    """Resolve image names from command params.

    Supports both:
    - ``images``: list of image names (Java flow format)
    - ``image_name``: single image name (Python flow format)

    Returns a non-empty list of interpolated image name strings.
    """
    # Try 'images' list first (Java imported flows)
    images_param = cmd.params.get("images")
    if isinstance(images_param, list) and images_param:
        return [ctx.interpolate(n) for n in images_param if n]

    # Fall back to single 'image_name'
    image_name = ctx.interpolate(cmd.params.get("image_name", ""))
    if image_name:
        return [image_name]

    return []


def _resolve_anchor_image_names(cmd: FlowCommand, ctx: FlowContext) -> list[str]:
    """Resolve the separate template list used to locate an image-search scope."""
    images = cmd.params.get("search_anchor_images")
    if not isinstance(images, list):
        return []
    return [ctx.interpolate(name) for name in images if isinstance(name, str) and name]


def _find_any_image(
    ctx: FlowContext,
    image_names: list[str],
    screenshot: bytes,
    threshold: float,
    color_match: bool = False,
    color_tolerance: float = 0.3,
    *,
    match_params: dict[str, object] | None = None,
    choose_best: bool = False,
    log_match: bool = True,
) -> tuple[str | None, object | None]:
    """Try to find any of the given template images in the screenshot.

    Returns (matched_name, result) where result has center_x, center_y, confidence.
    Returns (None, None) if no image matched.
    """
    db = ctx._executor._db
    if db is None:
        raise RuntimeError("database not available")

    active_command = getattr(ctx, "active_command", None)
    params = (
        match_params
        if match_params is not None
        else getattr(active_command, "params", {})
    )
    resolver = getattr(ctx, "resolve_command_scalar", None)

    def resolve_value(name: str, default: object, value_type: ValueType) -> object:
        value = params.get(name, default) if isinstance(params, dict) else default
        return resolver(value, value_type) if callable(resolver) else value

    try:
        from PIL import Image

        surface_size = Image.open(BytesIO(screenshot)).size
    except Exception as exc:
        raise RuntimeError("Image search screenshot is invalid.") from exc
    pixel_scale = (1.0, 1.0)
    if ctx.resolution_scaling_active:
        transform = ctx.resolution_transform(surface_size)
        if transform.scale_pixel_geometry:
            pixel_scale = (transform.x_scale, transform.y_scale)

    def match_stored(
        image_name: str,
        search_png: bytes,
        *,
        match_threshold: float,
        match_color: bool,
        match_tolerance: float,
    ) -> tuple[bool, object | None]:
        return find_stored_image(
            ctx,
            db,
            _find_image_service,
            search_png,
            image_name,
            surface_size,
            threshold=match_threshold,
            color_match=match_color,
            color_tolerance=match_tolerance,
        )

    def match_anchor(image_name: str, search_png: bytes) -> object | None:
        anchor_threshold = float(
            resolve_value("search_anchor_threshold", 0.9, ValueType.NUMBER)
        )
        anchor_color_match = bool(
            resolve_value("search_anchor_color_match", False, ValueType.BOOLEAN)
        )
        anchor_color_tolerance = float(
            resolve_value(
                "search_anchor_color_tolerance", 0.3, ValueType.NUMBER
            )
        )
        _exists, result = match_stored(
            image_name,
            search_png,
            match_threshold=anchor_threshold,
            match_color=anchor_color_match,
            match_tolerance=anchor_color_tolerance,
        )
        if result is not None and log_match:
            ctx.log_detailed(
                "Search anchor — "
                + format_image_match(
                    image_name,
                    result,
                    threshold=anchor_threshold,
                    color_match=anchor_color_match,
                    color_tolerance=anchor_color_tolerance,
                )
            )
        return result

    try:
        search_surface = prepare_search_region(
            screenshot,
            params if isinstance(params, dict) else {},
            db,
            resolve_value=resolve_value,
            anchor_image_names=[
                ctx.interpolate(name)
                for name in params.get("search_anchor_images", [])
                if isinstance(name, str) and name
            ]
            if isinstance(params, dict)
            else [],
            pixel_scale=pixel_scale,
            match_image=match_anchor,
        )
    except ImageSearchRegionError as exc:
        raise RuntimeError(str(exc)) from exc

    best_name: str | None = None
    best_result: object | None = None
    for image_name in image_names:
        exists, result = match_stored(
            image_name,
            search_surface.png,
            match_threshold=threshold,
            match_color=color_match,
            match_tolerance=color_tolerance,
        )
        if not exists:
            if ctx.listener:
                ctx.listener.on_log(f"Image '{image_name}' not found in database, skipping")
            else:
                logger.debug("Image '%s' not found in database, skipping", image_name)
            continue
        if result is not None:
            absolute = to_absolute_match(result, search_surface)
            if not choose_best:
                if log_match:
                    ctx.log_detailed(
                        format_image_match(
                            image_name,
                            absolute,
                            threshold=threshold,
                            color_match=color_match,
                            color_tolerance=color_tolerance,
                        )
                    )
                return image_name, absolute
            if best_result is None or float(absolute.confidence) > float(
                best_result.confidence
            ):
                best_name = image_name
                best_result = absolute

    if (
        best_name is not None
        and best_result is not None
        and log_match
    ):
        ctx.log_detailed(
            format_image_match(
                best_name,
                best_result,
                threshold=threshold,
                color_match=color_match,
                color_tolerance=color_tolerance,
            )
        )
    elif best_result is None and log_match:
        color_detail = (
            f"on; tolerance={color_tolerance:.4f}"
            if color_match
            else "off"
        )
        ctx.log_trace(
            f"Image search: no match across {len(image_names)} template(s); "
            f"threshold={threshold:.4f}; color check={color_detail}"
        )
    return best_name, best_result


def _take_screenshot_for_device(
    ctx: FlowContext,
    *,
    dispatch_hooks: bool = True,
) -> bytes | None:
    """Take a screenshot using the appropriate method for the current device type."""
    while True:
        device = ctx.session.device
        screenshot_capability = get_device_capability(
            ctx.session,
            ScreenshotCapability,
        )

        if screenshot_capability is not None:
            screenshot = screenshot_capability.take_screenshot()
        elif isinstance(device, BrowserDevice):
            session_data = ctx.session.data["browser_data"]
            from automadin.capabilities.browser.port import browser_automation_port

            screenshot = browser_automation_port(session_data).screenshot()
        elif isinstance(device, WindowsDevice):
            win_data = ctx.session.data["windows_data"]
            win_engine = WindowsDeviceEngine()
            screenshot = win_engine.take_screenshot(win_data)
        else:
            return None

        command = getattr(ctx, "active_command", None)
        executor = getattr(ctx, "_executor", None)
        if (
            not dispatch_hooks
            or ctx.hooks_suspended
            or not isinstance(command, FlowCommand)
            or executor is None
            or not executor.hook_dispatcher.supports(
                command.id,
                HookType.VISUAL_IMAGE,
                HookPhase.BEFORE,
            )
        ):
            return screenshot

        from automadin.execution.hooks import dispatch_visual_hooks

        if not dispatch_visual_hooks(command, ctx, HookPhase.BEFORE, screenshot):
            return screenshot


def handle_screenshot(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Take a screenshot from the current device (browser, android, or windows)."""
    import re
    from datetime import datetime
    from pathlib import Path

    screenshot_bytes = _take_screenshot_for_device(ctx)
    if screenshot_bytes is None:
        raise RuntimeError("Failed to take screenshot: unsupported device type or error")
    ctx.set_variable("_last_screenshot", "captured")
    ctx._last_screenshot = screenshot_bytes
    if ctx.listener:
        ctx.listener.on_log("Screenshot captured")

    raw_save_to_disk = cmd.params.get("save_to_disk", False)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    save_to_disk = bool(
        resolver(raw_save_to_disk, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_save_to_disk
    )
    if not save_to_disk:
        return

    save_path = str(cmd.params.get("save_path", "")).strip()
    if not save_path:
        raise RuntimeError("screenshot: 'Save Directory' is required when 'Save to Disk' is enabled")

    save_dir = Path(save_path)
    if not save_dir.is_dir():
        raise RuntimeError(f"screenshot: directory does not exist: {save_path}")

    device = ctx.session.device
    raw_name = getattr(device, "name", None) or getattr(device, "id", None) or "device"
    safe_name = re.sub(r"[^\w\-]", "_", str(raw_name)).strip("_") or "device"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{safe_name}-{ts}.png"
    filepath = save_dir / filename
    filepath.write_bytes(screenshot_bytes)

    if ctx.listener:
        ctx.listener.on_log(f"Screenshot saved: {filepath}")


def handle_read_screen_text(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Capture a fixed or image-anchored region and write an OCR result.

    The recognizer is supplied by the active session through the
    ``ScreenTextOcrEngine`` interface, keeping command execution independent
    from a particular OCR provider.
    """
    screenshot = _take_screenshot_for_device(ctx)
    if screenshot is None:
        raise RuntimeError("read_screen_text: screenshot capture is unavailable")
    try:
        from PIL import Image

        Image.open(BytesIO(screenshot)).verify()
    except Exception as exc:
        raise RuntimeError("read_screen_text: captured screenshot is invalid") from exc
    resolver = getattr(ctx, "resolve_command_scalar", None)
    engine = ctx.session.data.get("screen_text_ocr_engine")
    if not isinstance(engine, ScreenTextOcrEngine):
        raise RuntimeError(
            "read_screen_text: no OCR engine is configured for this session"
        )
    def resolve_value(name: str, default: object, value_type: ValueType) -> object:
        raw_value = cmd.params.get(name, default)
        return resolver(raw_value, value_type) if callable(resolver) else raw_value

    db = getattr(ctx._executor, "_db", None)
    from PIL import Image

    surface_size = Image.open(BytesIO(screenshot)).size
    pixel_scale = (1.0, 1.0)
    if ctx.resolution_scaling_active:
        transform = ctx.resolution_transform(surface_size)
        if transform.scale_pixel_geometry:
            pixel_scale = (transform.x_scale, transform.y_scale)

    def match_anchor(image_name: str, search_png: bytes) -> object | None:
        if db is None:
            return None
        _exists, match = find_stored_image(
            ctx,
            db,
            _find_image_service,
            search_png,
            image_name,
            surface_size,
            threshold=float(resolve_value("threshold", 0.9, ValueType.NUMBER)),
            color_match=bool(
                resolve_value("color_match", False, ValueType.BOOLEAN)
            ),
            color_tolerance=float(
                resolve_value("color_tolerance", 0.3, ValueType.NUMBER)
            ),
        )
        return match

    def log_recognized_text(recognized_text: str) -> None:
        if ctx.listener:
            ctx.listener.on_log(
                f"Read Screen Text: OCR raw={format_runtime_value(recognized_text)}"
            )

    log_values = cmd.params.get("log_values", False) is True
    try:
        result = read_screen_text(
            screenshot,
            cmd.params,
            db,
            engine,
            resolve_value=resolve_value,
            image_names=_resolve_image_names(cmd, ctx),
            pixel_scale=pixel_scale,
            match_image=match_anchor,
            on_recognized=log_recognized_text if log_values else None,
        )
    except ScreenTextReadingError as exc:
        raise RuntimeError(str(exc)) from exc
    if result.prepared.matched_image is not None and ctx.listener:
        ctx.listener.on_log(
            f"Read screen text anchored to image '{result.prepared.matched_image}'"
        )
    target = cmd.params.get("variable")
    ctx.set_output_variable(target, result.value)  # type: ignore[arg-type]
    if ctx.listener:
        box = result.prepared.box
        target_name = variable_display_name(
            target,
            ctx.flow_values.catalog if ctx.flow_values is not None else None,
        )
        result_type = str(cmd.params.get("result_type", "text")).casefold()
        detail = (
            f"parsed {result_type}={format_runtime_value(result.value)}"
            if log_values
            else f"parsed {result_type}; value hidden"
        )
        ctx.listener.on_log(
            "Read Screen Text: "
            f"region=({box.left}, {box.top})–({box.right}, {box.bottom}); "
            f"{detail}; "
            f"stored in Variable '{target_name}'"
        )


def handle_find_image(cmd: FlowCommand, ctx: FlowContext) -> None:
    """Find a template image within the current screen.

    Params:
        images: list of template image names (Java flow format).
        image_name: single template image name (Python flow format).
        threshold: match confidence threshold (0.0-1.0), default 0.90.

    Sets context variables:
        _image_found: "true" or "false"
        _match_x: center X of the match (or "0")
        _match_y: center Y of the match (or "0")
        _match_confidence: confidence value (or "0")
    """
    image_names = _resolve_image_names(cmd, ctx)
    resolver = getattr(ctx, "resolve_command_scalar", None)
    raw_threshold = cmd.params.get("threshold", 0.90)
    threshold = float(
        resolver(raw_threshold, ValueType.NUMBER)
        if callable(resolver)
        else raw_threshold
    )
    raw_color_match = cmd.params.get("color_match", False)
    color_match = bool(
        resolver(raw_color_match, ValueType.BOOLEAN)
        if callable(resolver)
        else raw_color_match
    )
    raw_color_tolerance = cmd.params.get("color_tolerance", 0.3)
    color_tolerance = float(
        resolver(raw_color_tolerance, ValueType.NUMBER)
        if callable(resolver)
        else raw_color_tolerance
    )

    if not image_names:
        raise RuntimeError("find_image: 'images' or 'image_name' parameter is required")

    screenshot = ctx._last_screenshot
    while True:
        if screenshot is None:
            screenshot = _take_screenshot_for_device(ctx)
        if screenshot is None:
            raise RuntimeError("find_image: no screenshot available")
        ctx._last_screenshot = screenshot
        matched_name, result = _find_any_image(
            ctx, image_names, screenshot, threshold, color_match, color_tolerance
        )
        if result is not None:
            break
        resolution = resolve_not_found_policy(
            ctx,
            cmd,
            f"Could not find {', '.join(image_names)}. Make it visible, then choose Retry.",
        )
        if resolution is AttentionResolution.RETRY and not ctx.is_stopped:
            screenshot = None
            continue
        if resolution is AttentionResolution.STOP:
            ctx.stop()
            return
        break

    if result is not None:
        ctx.set_variable("_image_found", "true")
        ctx.set_variable("_match_x", str(result.center_x))
        ctx.set_variable("_match_y", str(result.center_y))
        ctx.set_variable("_match_confidence", f"{result.confidence:.4f}")
    else:
        ctx.set_variable("_image_found", "false")
        ctx.set_variable("_match_x", "0")
        ctx.set_variable("_match_y", "0")
        ctx.set_variable("_match_confidence", "0")
        if ctx.listener:
            ctx.listener.on_log(f"No image found from {image_names} (threshold={threshold})")
