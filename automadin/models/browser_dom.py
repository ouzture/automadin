"""Validated browser DOM search scopes shared by runtime and editor tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from automadin.models.locator import LocatorSpec


@dataclass(frozen=True)
class BrowserDomScope:
    """Optional container that narrows a browser element locator."""

    container: LocatorSpec | None = None

    @classmethod
    def parse(cls, value: object = None) -> BrowserDomScope:
        if isinstance(value, cls):
            return value
        if value in (None, "", {}):
            return cls()
        if isinstance(value, Mapping) and "mode" in value:
            unknown = set(value).difference({"mode", "container"})
            if unknown:
                names = ", ".join(sorted(str(name) for name in unknown))
                raise ValueError(f"Browser DOM scope contains unknown field(s): {names}")
            mode = value.get("mode")
            if mode == "page":
                if value.get("container") not in (None, "", {}):
                    raise ValueError("Page DOM scope cannot contain a container")
                return cls()
            if mode != "container":
                raise ValueError("Browser DOM scope mode must be 'page' or 'container'")
            container = value.get("container")
            if container in (None, "", {}):
                raise ValueError("Container DOM scope requires a locator")
            return cls(container=LocatorSpec.parse(container))
        return cls(container=LocatorSpec.parse(value))

    def to_dict(self) -> dict[str, Any]:
        if self.container is None:
            return {"mode": "page"}
        return {"mode": "container", "container": self.container.to_dict()}


@dataclass(frozen=True)
class BrowserElementLoopItem:
    """Opaque reference to one element in an owner-thread collection snapshot."""

    snapshot_token: str
    index: int
    count: int


__all__ = ["BrowserDomScope", "BrowserElementLoopItem"]
