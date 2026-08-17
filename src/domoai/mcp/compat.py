"""Narrow compatibility repairs for supported MCP SDK versions."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from pydantic.errors import PydanticUndefinedAnnotation


def ensure_fastmcp_settings_ready() -> bool:
    """Resolve FastMCP's generic lifespan field when the SDK left it incomplete.

    Returns ``True`` only when a rebuild was attempted successfully. The helper
    does not install a global warning filter and does not catch server
    construction failures unrelated to the known forward-reference problem.
    """

    try:
        fastmcp_server: Any = importlib.import_module("mcp.server.fastmcp.server")
        FastMCP: Any = fastmcp_server.FastMCP
        LifespanResultT: Any = getattr(fastmcp_server, "LifespanResultT", object)
        Settings: Any = fastmcp_server.Settings
    except ImportError:
        return False

    lifespan = Settings.model_fields.get("lifespan")
    if lifespan is None or getattr(lifespan, "_complete", True):
        return False

    try:
        Settings.model_rebuild(
            _types_namespace={
                "FastMCP": FastMCP,
                "LifespanResultT": LifespanResultT,
                "Callable": Callable,
                "AbstractAsyncContextManager": AbstractAsyncContextManager,
            }
        )
    except (NameError, PydanticUndefinedAnnotation, TypeError):
        return False
    return bool(getattr(Settings.model_fields.get("lifespan"), "_complete", False))
