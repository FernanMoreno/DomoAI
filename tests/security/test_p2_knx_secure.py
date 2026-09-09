from __future__ import annotations

import json
from pathlib import Path

import pytest

from domoai.application.knx_secure import KnxSecureCredentialStore, KnxSecureError
from domoai.security.secrets import FileSecretStore


def test_knx_secure_credentials_are_encrypted_and_rotatable(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    credentials = KnxSecureCredentialStore(store)
    old_ref = credentials.put(
        keyring_password="old-keyring",
        user_password="old-user",
        device_authentication_password="old-device",
        backbone_key="00" * 16,
    )

    new_ref = credentials.rotate(
        old_ref,
        keyring_password="new-keyring",
        user_password="new-user",
        device_authentication_password="new-device",
        backbone_key="11" * 16,
    )

    assert new_ref != old_ref
    assert credentials.resolve(new_ref).user_password == "new-user"
    assert credentials.resolve(old_ref).user_password == "old-user"
    raw = " ".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "secrets").glob("*.json")
    )
    assert "old-user" not in raw
    assert "new-user" not in raw


def test_knx_secure_credentials_reject_malformed_payload(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "master.key")
    reference = store.put(json.dumps({"user_password": "missing required shape"}))

    with pytest.raises(KnxSecureError, match="credentials"):
        KnxSecureCredentialStore(store).resolve(reference)
