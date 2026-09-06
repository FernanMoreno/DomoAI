"""Typed identity helpers for safe, ordered semantic scenes."""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from domoai.application.bundle_commit import BundleCommitRequestMember
from domoai.domain.models import StrictModel


class SceneCommitRequest(StrictModel):
    """Request envelope for one scene routed through the bundle aggregate."""

    scene_id: str = Field(min_length=1, max_length=200)
    scene_digest: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1, max_length=200)
    runtime_revision: str = Field(min_length=1)
    bundle_digest: str = Field(min_length=1)
    members: list[BundleCommitRequestMember] = Field(min_length=1, max_length=50)


def scene_commit_digest(
    *,
    scene_id: str,
    scenario_id: str,
    runtime_revision: str,
    bundle_digest: str,
    members: list[BundleCommitRequestMember],
) -> str:
    """Return the immutable identity of a named ordered scene request."""

    payload = {
        "schema": "scene-commit-v1",
        "scene_id": scene_id,
        "scenario_id": scenario_id,
        "runtime_revision": runtime_revision,
        "bundle_digest": bundle_digest,
        "members": [member.model_dump(mode="json", exclude_none=True) for member in members],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


__all__ = ["SceneCommitRequest", "scene_commit_digest"]
