"""Child-only typed Built-ins shared by all image-condition commands.

Future image-condition handlers must use :func:`image_match_child_scope` rather
than constructing ad-hoc coordinate scopes, so real-match and lifetime semantics
remain consistent across conditional and looping commands.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from automadin.execution.context import FlowContext
from automadin.execution.flow_values import RuntimeTypedValue
from automadin.models.typed_value import ValueType


@contextmanager
def image_match_child_scope(
    ctx: FlowContext,
    coordinates: tuple[int, int] | None,
) -> Iterator[None]:
    """Expose one real match centre only while its child block executes."""
    if ctx.flow_values is None or coordinates is None:
        yield
        return

    x, y = coordinates
    with ctx.flow_values.builtin_scope(
        {
            "_match_x": RuntimeTypedValue(ValueType.INTEGER, int(x)),
            "_match_y": RuntimeTypedValue(ValueType.INTEGER, int(y)),
        }
    ):
        yield


__all__ = ["image_match_child_scope"]
