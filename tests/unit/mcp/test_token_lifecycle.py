import asyncio
import json
from pathlib import Path

from domoai.mcp.auth import StaticBearerTokenVerifier
from domoai.mcp.token_lifecycle import TokenFileManager


def test_rotation_is_atomic_and_revoke_survives_reload(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "clients.json"
    manager = TokenFileManager(path)

    first = manager.rotate("agent", scopes=["read", "mutate"], household_ids=["home-a"])
    verifier = StaticBearerTokenVerifier.from_file(path)
    assert asyncio.run(verifier.verify_token(first.token)) is not None

    second = manager.rotate("agent", scopes=["read"], household_ids=["home-a"])
    verifier.reload()
    assert asyncio.run(verifier.verify_token(first.token)) is None
    assert asyncio.run(verifier.verify_token(second.token)) is not None

    assert manager.revoke("agent") is True
    verifier.reload()
    assert asyncio.run(verifier.verify_token(second.token)) is None
    assert second.token not in repr(second)
    assert path.stat().st_mode & 0o077 == 0


def test_token_claims_include_non_secret_identity_scope(tmp_path: Path) -> None:
    path = tmp_path / "clients.json"
    result = TokenFileManager(path).rotate(
        "planner",
        scopes=["read", "plan"],
        household_ids=["home-a", "home-b"],
        device_ids=["light.one"],
    )
    access = asyncio.run(StaticBearerTokenVerifier.from_file(path).verify_token(result.token))

    assert access is not None
    assert access.claims["authority"]["household_ids"] == ["home-a", "home-b"]
    assert access.claims["authority"]["device_ids"] == ["light.one"]
    assert result.token not in json.dumps(access.claims)
