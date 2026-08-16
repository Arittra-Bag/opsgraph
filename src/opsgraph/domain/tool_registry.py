"""Tool registry with undeletable native capabilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

CORE_SCHEMA_INSPECT = "core.schema.inspect"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    handler: Callable[..., object]
    native: bool = False


class ToolRegistry:
    """In-process alpha registry; native tools cannot be replaced or removed."""

    def __init__(self, schema_inspector: Callable[..., object]) -> None:
        self._tools: dict[str, ToolDefinition] = {
            CORE_SCHEMA_INSPECT: ToolDefinition(
                name=CORE_SCHEMA_INSPECT,
                description="Inspect a parsed schema snapshot without reading row data.",
                handler=schema_inspector,
                native=True,
            )
        }

    @property
    def tools(self) -> Mapping[str, ToolDefinition]:
        return dict(self._tools)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name == CORE_SCHEMA_INSPECT or tool.name in self._tools:
            raise ValueError(f"tool name is reserved or already registered: {tool.name}")
        if tool.native:
            raise ValueError("extensions cannot register themselves as native")
        self._tools[tool.name] = tool

    def remove(self, name: str) -> None:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(name)
        if tool.native:
            raise ValueError(f"native tool cannot be removed: {name}")
        del self._tools[name]
