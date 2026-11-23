from __future__ import annotations

import pytest

from automadin.models.flow import AutomationFlow
from automadin.models.resolution_scaling import (
    ResolutionScalingMode,
    ResolutionScalingOverride,
    ResolutionScalingSettings,
)
from automadin.models.run_config import RunConfig


def test_legacy_flow_defaults_to_resolution_scaling_off() -> None:
    flow = AutomationFlow.from_dict({"name": "Legacy", "commands": []})

    assert flow.resolution_scaling == ResolutionScalingSettings()
    assert flow.resolution_scaling.mode is ResolutionScalingMode.OFF


def test_flow_resolution_scaling_round_trips() -> None:
    flow = AutomationFlow(
        "Scaled",
        resolution_scaling=ResolutionScalingSettings(
            ResolutionScalingMode.ADAPTIVE,
            1920,
            1080,
            scale_pixel_geometry=False,
        ),
    )

    assert AutomationFlow.from_dict(flow.to_dict()) == flow


def test_adaptive_flow_requires_a_reference_surface() -> None:
    with pytest.raises(ValueError, match="requires reference"):
        ResolutionScalingSettings(ResolutionScalingMode.ADAPTIVE)


def test_legacy_run_config_inherits_flow_scaling() -> None:
    config = RunConfig.from_dict({"mode": "once"})

    assert config.resolution_scaling_override is ResolutionScalingOverride.INHERIT
    assert config.custom_scale_percent == 100


def test_run_config_resolution_override_round_trips() -> None:
    config = RunConfig(
        resolution_scaling_override=ResolutionScalingOverride.CUSTOM,
        custom_scale_percent=125,
    )

    assert RunConfig.from_dict(config.to_dict()) == config
