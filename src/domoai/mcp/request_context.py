"""Per-request identity propagation from MCP transport to runtime execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps

from domoai.mcp.auth import current_client_id
from domoai.runtime.execution_context import execution_principal


def with_request_principal[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Keep authenticated client identity local to one async tool invocation."""

    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with execution_principal(current_client_id()):
            return await function(*args, **kwargs)

    return wrapped
