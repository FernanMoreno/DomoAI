from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from domoai.security.secrets import (
    FileSecretStore,
    SecretPermissionError,
    SecretReferenceError,
    SecretUnavailableError,
)


def test_put_encrypts_secret_and_resolves_after_restart(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    reference = store.put("ha-token-plaintext")

    assert reference.startswith("secret:")
    assert "ha-token-plaintext" not in repr(store)
    assert all(
        "ha-token-plaintext" not in path.read_text(encoding="utf-8", errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert FileSecretStore(tmp_path / "master.key").resolve(reference) == "ha-token-plaintext"


def test_secret_files_and_master_key_are_owner_only(tmp_path: Path) -> None:
    reference = FileSecretStore(tmp_path / "master.key").put("value")

    assert stat.S_IMODE((tmp_path / "master.key").stat().st_mode) == 0o600
    assert stat.S_IMODE(
        next((tmp_path / "secrets").glob("*.json")).stat().st_mode
    ) == 0o600
    assert reference


def test_insecure_existing_master_key_fails_closed(tmp_path: Path) -> None:
    key_path = tmp_path / "master.key"
    key_path.write_bytes(os.urandom(32))
    key_path.chmod(0o644)

    with pytest.raises(SecretPermissionError, match="permissions"):
        FileSecretStore(key_path)


def test_environment_reference_is_resolved_without_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMOAI_TEST_TOKEN", "environment-secret")
    store = FileSecretStore(tmp_path / "master.key")

    assert store.resolve("env:DOMOAI_TEST_TOKEN") == "environment-secret"
    assert not (tmp_path / "secrets").exists()


def test_missing_or_malformed_references_are_typed_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    monkeypatch.delenv("DOMOAI_MISSING_TOKEN", raising=False)

    with pytest.raises(SecretUnavailableError) as missing:
        store.resolve("env:DOMOAI_MISSING_TOKEN")
    assert "environment-secret" not in str(missing.value)

    with pytest.raises(SecretReferenceError):
        store.resolve("token:environment-secret")


def test_corrupt_payload_does_not_expose_ciphertext(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    reference = store.put("secret-value")
    payload_path = next((tmp_path / "secrets").glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["ciphertext"] = "not-valid-ciphertext"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SecretUnavailableError) as error:
        store.resolve(reference)
    assert "not-valid-ciphertext" not in str(error.value)
    assert "secret-value" not in str(error.value)
