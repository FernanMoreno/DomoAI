from __future__ import annotations

import warnings
from typing import Any, cast

from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning

from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.mcp.ortools_server import OrtoolsMcpContext, create_ortools_server


def test_both_mcp_servers_construct_without_incomplete_settings_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", IncompleteFieldDefinitionWarning)
        create_domotics_server(
            DomoticsMcpContext(
                discovery=cast(Any, None),
                state_service=cast(Any, None),
                facade=cast(Any, None),
                registry=cast(Any, None),
                policies=[],
            )
        )
        create_ortools_server(
            OrtoolsMcpContext(
                registry=cast(Any, None),
                plan_service=cast(Any, None),
                optimization_service=cast(Any, None),
            )
        )
