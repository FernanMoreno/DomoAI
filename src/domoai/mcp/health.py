"""Small, sanitized health and readiness reports for the shared gateway."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from starlette.requests import Request
from starlette.responses import JSONResponse


async def _adapter_health(runtime: Any) -> Mapping[str, Any]:
    adapter = getattr(runtime, "adapter", None)
    if adapter is not None:
        health = await adapter.health()
    else:
        health = await runtime.health()
    return cast(Mapping[str, Any], health.model_dump(mode="python"))


def _safe_adapter_health(health: Mapping[str, Any]) -> dict[str, Any]:
    """Expose topology and status without forwarding provider messages."""

    connected = bool(health.get("connected", False))
    components = health.get("components")
    safe_components = None
    if isinstance(components, list):
        safe_components = [
            _safe_adapter_health(component)
            for component in components
            if isinstance(component, Mapping)
        ]
    if safe_components is not None and any(
        not bool(component.get("connected", False)) for component in safe_components
    ):
        status = "degraded"
    else:
        status = "ready" if connected else "unavailable"
    payload: dict[str, Any] = {
        "adapter_id": str(health.get("adapter_id", "unknown")),
        "connected": connected,
        "status": status,
    }
    if safe_components is not None:
        payload["components"] = safe_components
    return payload


async def _freshness_report(runtime: Any) -> dict[str, Any]:
    state_store = getattr(runtime, "state_store", None)
    if state_store is None or not hasattr(state_store, "all"):
        return {"status": "unknown", "max_age_seconds": None}
    snapshots = await state_store.all()
    optional_node_ids = getattr(getattr(runtime, "settings", None), "matter_optional_node_ids", ())
    optional_sources = frozenset(
        (snapshot.source_ref.adapter_id, snapshot.source_ref.external_id)
        for snapshot in snapshots
        if snapshot.source_ref.adapter_id == "matter"
        and any(
            snapshot.source_ref.external_id.startswith(f"node:{node_id}/endpoint:")
            for node_id in optional_node_ids
        )
    )
    if hasattr(state_store, "freshness_report"):
        return dict(state_store.freshness_report(optional_sources=optional_sources))
    return {"status": "unknown", "max_age_seconds": None}


def _physical_readiness(runtime: Any) -> dict[str, str]:
    qualification = str(getattr(runtime, "battery_qualification", "unsupported"))
    operational_status = getattr(runtime, "battery_operational_status", None)
    payload: dict[str, str] = {
        "status": "ready" if qualification in {"unsupported", "hil-qualified"} else "not_ready",
        "battery_qualification": qualification,
    }
    if operational_status is not None:
        payload["battery_operational_status"] = str(operational_status)
    return payload


async def healthz(_: Request) -> JSONResponse:
    """Return process liveness without exposing configuration or secrets."""

    return JSONResponse({"service": "domoai", "status": "ok"})


async def readyz(runtime: Any, _: Request) -> JSONResponse:
    """Report whether the shared runtime can accept MCP work."""

    lifecycle = runtime.lifecycle
    try:
        adapter = _safe_adapter_health(await _adapter_health(runtime))
        adapter_ready = (
            bool(adapter.get("connected", False)) and adapter.get("status") == "ready"
        )
    except Exception:  # noqa: BLE001 - readiness must fail closed and stay safe
        adapter = {"adapter_id": "unknown", "connected": False, "status": "unavailable"}
        adapter_ready = False

    lifecycle_ready = bool(
        getattr(lifecycle, "started", False)
        and not getattr(lifecycle, "closed", False)
        and getattr(lifecycle, "running_task_count", 0) >= 2
    )
    ownership = getattr(runtime, "ownership", None)
    ownership_ready = ownership is None or not bool(getattr(ownership, "released", True))
    physical = _physical_readiness(runtime)
    freshness = await _freshness_report(runtime)
    has_state_store = getattr(runtime, "state_store", None) is not None
    freshness_ready = not has_state_store or freshness.get("status") == "current"
    is_ready = (
        lifecycle_ready
        and adapter_ready
        and ownership_ready
        and physical["status"] == "ready"
        and freshness_ready
    )
    reason_codes: list[str] = []
    if not lifecycle_ready:
        reason_codes.append("runtime_not_started")
    if not adapter_ready:
        reason_codes.append("adapter_not_ready")
    components = adapter.get("components")
    if isinstance(components, list) and any(
        isinstance(component, Mapping)
        and str(component.get("adapter_id")) == "knx"
        and not bool(component.get("connected", False))
        for component in components
    ):
        reason_codes.append("knx_unavailable")
    if not ownership_ready:
        reason_codes.append("runtime_ownership_inactive")
    if physical["status"] != "ready":
        reason_codes.append("physical_actuator_not_qualified")
    if physical.get("battery_operational_status") == "observed-only":
        reason_codes.append("battery_dispatch_binding_missing")
    if has_state_store and freshness.get("status") != "current":
        reason_codes.extend(
            code for code in freshness.get("reason_codes", []) if code not in reason_codes
        )
        if freshness.get("status") == "unknown":
            reason_codes.append("state_freshness_unknown")
    payload = {
        "service": "domoai",
        "status": "ready" if is_ready else "not_ready",
        "runtime": {
            "status": "ready" if lifecycle_ready else "not_ready",
            "started": bool(getattr(lifecycle, "started", False)),
            "running_tasks": int(getattr(lifecycle, "running_task_count", 0)),
        },
        "adapter": adapter,
        "ownership": {"active": ownership_ready},
        "authorization": {
            "status": (
                "configured"
                if getattr(runtime.settings, "mcp_client_token_file", None)
                else "local"
            )
        },
        "freshness": freshness,
        "physical": physical,
        "reason_codes": reason_codes,
    }
    return JSONResponse(payload, status_code=200 if is_ready else 503)
