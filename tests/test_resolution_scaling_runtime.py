from __future__ import annotations

from automadin.execution.context import FlowContext
from automadin.execution.coordinates import resolve_offset, resolve_point
from automadin.execution.executor import FlowExecutor
from automadin.execution.runner import FlowRunWorker
from automadin.models.device import DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.resolution_scaling import (
    ResolutionScalingMode,
    ResolutionScalingOverride,
    ResolutionScalingSettings,
)
from automadin.models.run_config import RunConfig


def _settings(width: int = 1000, height: int = 500) -> ResolutionScalingSettings:
    return ResolutionScalingSettings(ResolutionScalingMode.ADAPTIVE, width, height)


def test_pixel_points_and_offsets_scale_but_percent_points_do_not() -> None:
    ctx = FlowContext(DeviceSession())
    with ctx.bind_resolution_scaling(_settings()):
        assert resolve_point(
            FlowCommand("click", params={"x": 100, "y": 100}),
            ctx,
            "x",
            "y",
            surface_size=(2000, 750),
        ) == (200, 150)
        assert resolve_offset(
            FlowCommand("click", params={"xmargin": 10, "ymargin": -10}),
            ctx,
            surface_size=(2000, 750),
        ) == (20, -15)
        assert resolve_point(
            FlowCommand(
                "click",
                params={"x": 50, "y": 50, "coordinate_unit": "percent"},
            ),
            ctx,
            "x",
            "y",
            surface_size=(2000, 750),
        ) == (1000, 375)


def test_live_match_builtin_coordinates_are_not_scaled_twice() -> None:
    flow = AutomationFlow("Values", value_model_version=1)
    from automadin.execution.flow_values import FlowInvocationValues, RuntimeTypedValue
    from automadin.models.typed_value import ValueType

    values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
    ctx = FlowContext(DeviceSession(), flow_values=values)
    command = FlowCommand(
        "click_position",
        params={
            "coordinate_unit": "pixels",
            "x": {"kind": "builtin", "name": "_match_x"},
            "y": {"kind": "builtin", "name": "_match_y"},
        },
    )
    with ctx.bind_resolution_scaling(_settings()), values.builtin_scope(
        {
            "_match_x": RuntimeTypedValue(ValueType.INTEGER, 250),
            "_match_y": RuntimeTypedValue(ValueType.INTEGER, 125),
        }
    ):
        assert resolve_point(
            command,
            ctx,
            "x",
            "y",
            surface_size=(2000, 750),
        ) == (250, 125)


def test_run_override_can_disable_or_apply_custom_scaling() -> None:
    ctx = FlowContext(
        DeviceSession(),
        resolution_scaling_override=ResolutionScalingOverride.OFF,
    )
    with ctx.bind_resolution_scaling(_settings()):
        assert resolve_point(
            FlowCommand("click", params={"x": 100, "y": 100}),
            ctx,
            "x",
            "y",
            surface_size=(2000, 1000),
        ) == (100, 100)

    ctx.resolution_scaling_override = ResolutionScalingOverride.CUSTOM
    ctx.custom_scale_percent = 125
    with ctx.bind_resolution_scaling(ResolutionScalingSettings()):
        assert resolve_point(
            FlowCommand("click", params={"x": 100, "y": 80}),
            ctx,
            "x",
            "y",
            surface_size=(10, 10),
        ) == (125, 100)


def test_flow_worker_applies_saved_run_scaling_override() -> None:
    ctx = FlowContext(DeviceSession())
    config = RunConfig(
        resolution_scaling_override=ResolutionScalingOverride.CUSTOM,
        custom_scale_percent=135,
    )
    worker = FlowRunWorker(FlowExecutor(), AutomationFlow("Flow"), ctx, config)
    try:
        assert ctx.resolution_scaling_override is ResolutionScalingOverride.CUSTOM
        assert ctx.custom_scale_percent == 135
    finally:
        worker.deleteLater()


def test_executor_restores_parent_scaling_after_a_subflow() -> None:
    observations: list[tuple[float, float]] = []
    child = AutomationFlow(
        "child",
        [FlowCommand("record")],
        resolution_scaling=_settings(500, 500),
    )
    parent = AutomationFlow(
        "parent",
        [FlowCommand("run_child"), FlowCommand("record")],
        resolution_scaling=_settings(1000, 500),
    )
    executor = FlowExecutor()

    def record(_cmd: FlowCommand, ctx: FlowContext) -> None:
        transform = ctx.resolution_transform((2000, 1000))
        observations.append((transform.x_scale, transform.y_scale))

    def run_child(_cmd: FlowCommand, ctx: FlowContext) -> None:
        executor.execute(child, ctx)

    executor.register_handler("record", record)
    executor.register_handler("run_child", run_child)
    executor.execute(parent, FlowContext(DeviceSession()))

    assert observations == [(4.0, 2.0), (2.0, 2.0)]
