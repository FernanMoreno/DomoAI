from __future__ import annotations

from domoai.admin.cli import main
from domoai.application.oauth_preflight import OAuthDeploymentPreflight


def test_oauth_preflight_dump_has_stable_secret_free_shape() -> None:
    result = OAuthDeploymentPreflight.from_environment({})
    payload = result.model_dump(mode="json")
    assert set(payload) == {
        "status",
        "missing_fields",
        "diagnostics",
        "external_registration_required",
        "configuration_fingerprint",
    }
    assert payload["diagnostics"] and all(
        "client_secret" not in str(item) for item in payload["diagnostics"]
    )


def test_oauth_preflight_is_available_from_admin_cli(tmp_path, capsys) -> None:
    env_file = tmp_path / "gateway.env"
    env_file.write_text(
        "DOMOAI_HOME_ASSISTANT_OAUTH_CLIENT_ID=client\n",
        encoding="utf-8",
    )
    assert main(["oauth", "preflight", "--env-file", str(env_file)]) == 0
    output = capsys.readouterr().out
    assert '"status": "manual_required"' in output
    assert '"client"' not in output
