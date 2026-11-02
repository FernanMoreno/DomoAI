"""Deterministic software qualification and explicit external gates."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import Field, model_validator

from domoai.domain.connector_recipes import ConnectorRecipe, TransportFamily
from domoai.domain.models import StrictModel

QualificationGateStatus = Literal["pending", "passed", "blocked"]


class QualificationGate(StrictModel):
    gate_id: str = Field(min_length=1, max_length=96)
    status: QualificationGateStatus = "pending"
    owner: str = Field(min_length=1, max_length=96)
    next_action: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_public_text(self) -> QualificationGate:
        if re.search(r"(?:token|password|secret|bearer|api[_-]?key)", self.owner, re.I):
            raise ValueError("qualification gate owner contains sensitive text")
        if re.search(r"(?:token|password|secret|bearer|api[_-]?key)", self.next_action, re.I):
            raise ValueError("qualification gate action contains sensitive text")
        return self


class QualificationReport(StrictModel):
    protocol: TransportFamily
    software_status: Literal["pass", "fail"]
    external_gates: tuple[QualificationGate, ...] = Field(min_length=1, max_length=16)
    secret_safe: Literal[True] = True
    evidence_digest: str = Field(min_length=7, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> QualificationReport:
        ids = [gate.gate_id for gate in self.external_gates]
        if len(ids) != len(set(ids)):
            raise ValueError("qualification gate IDs must be unique")
        if self.evidence_digest != self.digest():
            raise ValueError("qualification evidence digest does not match contents")
        return self

    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"evidence_digest"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class ConnectorQualificationService:
    """Create a report without contacting a device or provider."""

    def report_for_recipe(self, recipe: ConnectorRecipe) -> QualificationReport:
        gates = [
            QualificationGate(
                gate_id="real_device",
                owner="operator",
                next_action="Run the attended protocol qualification against the target device",
            )
        ]
        if recipe.transport_family is TransportFamily.MODBUS:
            gates[0] = gates[0].model_copy(
                update={
                    "gate_id": "modbus_device",
                    "next_action": (
                        "Verify register read/write and readback on the real Modbus device"
                    ),
                }
            )
        elif recipe.transport_family is TransportFamily.KNX:
            gates[0] = gates[0].model_copy(
                update={
                    "gate_id": "knx_bus",
                    "next_action": (
                        "Verify group reads, DPT conversion and readback on the real KNX bus"
                    ),
                }
            )
        elif recipe.transport_family is TransportFamily.MATTER:
            gates[0] = gates[0].model_copy(
                update={
                    "gate_id": "matter_commissioning",
                    "next_action": "Commission the node and verify declared endpoint interactions",
                }
            )
        if "oauth2" in {scheme.value for scheme in recipe.auth_schemes}:
            gates.append(
                QualificationGate(
                    gate_id="oauth_registration",
                    owner="provider_operator",
                    next_action=(
                        "Register the OAuth application and confirm the redirect URI externally"
                    ),
                )
            )
        report = QualificationReport.model_construct(
            protocol=recipe.transport_family,
            software_status="pass",
            external_gates=tuple(gates),
            evidence_digest="sha256:" + "0" * 64,
        )
        return report.model_copy(update={"evidence_digest": report.digest()})


__all__ = ["ConnectorQualificationService", "QualificationGate", "QualificationReport"]
