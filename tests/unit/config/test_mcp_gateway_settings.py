from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.config.settings import Settings


def test_gateway_defaults_are_loopback_and_use_the_mcp_path() -> None:
    settings = Settings()

    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 8000
    assert settings.mcp_path == "/mcp"
    assert settings.mcp_public_url == "http://127.0.0.1:8000"
    assert settings.mcp_json_response is True
    assert settings.mcp_server_sent_events is False
    assert settings.mcp_metrics_enabled is False
    assert settings.mcp_metrics_max_bytes == 262144


def test_gateway_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMOAI_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("DOMOAI_MCP_PORT", "9100")
    monkeypatch.setenv("DOMOAI_MCP_PATH", "/semantic-mcp")
    monkeypatch.setenv("DOMOAI_MCP_PUBLIC_URL", "https://home.example/mcp")
    monkeypatch.setenv("DOMOAI_MCP_CLIENT_TOKEN_FILE", "secrets/mcp-tokens.json")
    monkeypatch.setenv("DOMOAI_MCP_DEPLOYMENT_ID", "home-main")
    monkeypatch.setenv("DOMOAI_MCP_TENANT_ID", "tenant-main")
    monkeypatch.setenv("DOMOAI_MCP_HOUSEHOLD_ID", "home-main")
    monkeypatch.setenv("DOMOAI_PRIVACY_RETENTION_DAYS", "180")
    monkeypatch.setenv("DOMOAI_BACKUP_ENCRYPTION_KEY_FILE", "secrets/backup.key")
    monkeypatch.setenv("DOMOAI_MCP_JSON_RESPONSE", "false")
    monkeypatch.setenv("DOMOAI_MCP_SERVER_SENT_EVENTS", "true")
    monkeypatch.setenv("DOMOAI_MCP_METRICS_ENABLED", "true")
    monkeypatch.setenv("DOMOAI_MCP_METRICS_MAX_BYTES", "131072")

    settings = Settings.from_environment()

    assert settings.mcp_host == "0.0.0.0"
    assert settings.mcp_port == 9100
    assert settings.mcp_path == "/semantic-mcp"
    assert settings.mcp_public_url == "https://home.example/mcp"
    assert settings.mcp_client_token_file == Path("secrets/mcp-tokens.json")
    assert settings.mcp_deployment_id == "home-main"
    assert settings.mcp_tenant_id == "tenant-main"
    assert settings.mcp_household_id == "home-main"
    assert settings.privacy_retention_days == 180
    assert settings.backup_encryption_key_file == Path("secrets/backup.key")
    assert settings.mcp_json_response is False
    assert settings.mcp_server_sent_events is True
    assert settings.mcp_metrics_enabled is True
    assert settings.mcp_metrics_max_bytes == 131072


def test_metrics_enablement_requires_the_hash_only_client_token_file() -> None:
    with pytest.raises(ValidationError, match="metrics.*token file"):
        Settings(mcp_metrics_enabled=True)


def test_non_local_gateway_requires_bearer_tokens_and_https_public_url() -> None:
    with pytest.raises(ValidationError, match="client token"):
        Settings(mcp_host="0.0.0.0")

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            mcp_host="0.0.0.0",
            mcp_client_token_file=Path("tokens.json"),
            mcp_public_url="http://192.0.2.10:8000",
        )


def test_gateway_path_must_be_absolute_url_path() -> None:
    with pytest.raises(ValidationError, match="MCP path"):
        Settings(mcp_path="mcp")


def test_audit_database_must_be_physically_separate_from_authority_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "domoai.sqlite3"

    with pytest.raises(ValidationError, match="audit database"):
        Settings(database_path=database_path, audit_database_path=database_path)


def test_generic_mqtt_url_and_mapping_must_be_configured_together(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="GENERIC_MQTT"):
        Settings(generic_mqtt_url="mqtt://localhost:1883")

    with pytest.raises(ValidationError, match="GENERIC_MQTT"):
        Settings(generic_mqtt_mapping_path=tmp_path / "mapping.json")


def test_generic_mqtt_discovery_url_is_an_independent_read_only_source() -> None:
    settings = Settings(generic_mqtt_discovery_url="mqtt://localhost:1883")

    assert settings.generic_mqtt_discovery_url == "mqtt://localhost:1883"


def test_t012_a_resident_defaults_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOMOAI_RESIDENT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("DOMOAI_RESIDENT_ORIGIN", raising=False)
    assert Settings().resident_token_file is None
    assert Settings.from_environment().resident_origin is None
    monkeypatch.setenv("DOMOAI_RESIDENT_TOKEN_FILE", "private/my tokens.json")
    monkeypatch.setenv("DOMOAI_RESIDENT_ORIGIN", "HTTPS://HOME.Example:443")
    settings = Settings.from_environment()
    assert settings.resident_token_file == Path("private/my tokens.json")
    assert settings.resident_origin == "https://home.example"


def test_t012_a_resident_path_is_not_trimmed_or_resolved() -> None:
    value = " private/ resident.json "
    settings = Settings.model_validate(
        {"resident_token_file": value, "resident_origin": "https://home.example"}
    )
    assert settings.resident_token_file == Path(value)


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("HTTPS://HOME.Example:443", "https://home.example"),
        ("https://home.example:08443", "https://home.example:8443"),
        ("http://LOCALHOST:80", "http://localhost"),
        ("http://127.2.3.4:8000", "http://127.2.3.4:8000"),
        ("http://[0:0:0:0:0:0:0:1]:80", "http://[::1]"),
        ("https://[2001:0DB8::1]:443", "https://[2001:db8::1]"),
        ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example"),
    ],
)
def test_t012_a_origin_canonicalization(origin: str, expected: str) -> None:
    # A nonexistent path is legal here: Settings must not load/stat it.
    settings = Settings(resident_token_file=Path("missing resident.json"), resident_origin=origin)
    assert settings.resident_origin == expected


@pytest.mark.parametrize(
    "origin",
    [
        "",
        " ",
        " https://home.example",
        "https://home.example ",
        "https://home.example\n",
        "https://home.example/",
        "https://home.example?",
        "https://home.example#",
        "https://user@home.example",
        "https://*.example",
        "https://home.example.",
        "https://home..example",
        "https://-home.example",
        "https://home_.example",
        "https://bücher.example",
        "http://127.1",
        "https://12345",
        "https://home.example:0",
        "https://home.example:65536",
        "https://home.example:",
        "https://home.example:+80",
        "https://home.example:abc",
        "https://[::1%25eth0]",
        "https://home%2eexample",
        "https://home\\example",
        "ftp://home.example",
        "https://",
        "https://::1",
        "https://home.example/a",
        "https://" + "a" * 64 + ".example",
    ],
)
def test_t012_a_origin_rejects_ambiguous_syntax(origin: str) -> None:
    with pytest.raises(ValidationError, match="resident origin must be an absolute HTTP"):
        Settings(resident_token_file=Path("resident.json"), resident_origin=origin)


@pytest.mark.parametrize("missing", ["file", "origin"])
def test_t012_a_configuration_requires_pair(missing: str) -> None:
    with pytest.raises(
        ValidationError, match="resident token file and origin must be configured together"
    ):
        Settings(
            resident_token_file=None if missing == "file" else Path("resident.json"),
            resident_origin=None if missing == "origin" else "https://home.example",
        )


@pytest.mark.parametrize("field", ["DOMOAI_RESIDENT_TOKEN_FILE", "DOMOAI_RESIDENT_ORIGIN"])
@pytest.mark.parametrize("blank", ["", " \t"])
def test_t012_a_blank_environment_is_not_disabled(
    monkeypatch: pytest.MonkeyPatch, field: str, blank: str
) -> None:
    monkeypatch.setenv("DOMOAI_RESIDENT_TOKEN_FILE", "resident.json")
    monkeypatch.setenv("DOMOAI_RESIDENT_ORIGIN", "https://home.example")
    monkeypatch.setenv(field, blank)
    message = (
        "resident token file must not be blank"
        if field.endswith("FILE")
        else "resident origin must be an absolute HTTP"
    )
    with pytest.raises(ValidationError, match=message):
        Settings.from_environment()


@pytest.mark.parametrize(
    "bind,origin", [("0.0.0.0", "http://localhost"), ("127.0.0.1", "http://home.example")]
)
def test_t012_a_http_requires_both_loopbacks(bind: str, origin: str) -> None:
    with pytest.raises(
        ValidationError, match="resident HTTP origin requires loopback origin and bind"
    ):
        Settings(
            mcp_host=bind,
            mcp_public_url="https://home.example",
            mcp_client_token_file=Path("mcp.json"),
            resident_token_file=Path("resident.json"),
            resident_origin=origin,
        )


_RESIDENT_PATHS = (
    "/api/v1/resident/session",
    "/api/v1/resident/reviews",
    "/api/v1/resident/decisions",
)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/mcp/",
        "//mcp",
        "/mcp//x",
        "/mcp/./x",
        "/mcp/../x",
        "/.",
        "/..",
        "/mcp?x=1",
        "/mcp#frag",
        "/mc%70",
        "/m%2Fcp",
        "/mcp/ñ",
        "/mcp/é",
        "/{name}",
        "/{name:path}",
        "/mcp/{x}",
        "/mcp/*",
        "/mc*p",
        "/mcp:x",
        "/mcp" + chr(92) + "x",
        "/mc p",
        "/m\ncp",
    ],
)
def test_t012_b0_mcp_path_grammar_rejects_nonliteral_forms(path: str) -> None:
    with pytest.raises(ValueError, match="MCP path|at least 2 characters"):
        Settings(mcp_path=path)


@pytest.mark.parametrize(
    "path", ["/mcp", "/semantic-mcp", "/a/b_c.d~e", "/v1.2/MCP-9", "/..a", "/a.."]
)
def test_t012_b0_mcp_path_grammar_accepts_literal_paths(path: str) -> None:
    assert Settings(mcp_path=path).mcp_path == path


@pytest.mark.parametrize("path", _RESIDENT_PATHS)
@pytest.mark.parametrize("paired", [False, True])
def test_t012_b0_resident_paths_are_reserved_with_or_without_the_pair(
    path: str, paired: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = (
        {"resident_token_file": Path("resident.json"), "resident_origin": "https://home.example"}
        if paired
        else {}
    )
    with pytest.raises(ValueError, match="MCP path"):
        Settings(mcp_path=path, **extra)
    monkeypatch.setenv("DOMOAI_MCP_PATH", path)
    if paired:
        monkeypatch.setenv("DOMOAI_RESIDENT_TOKEN_FILE", "resident.json")
        monkeypatch.setenv("DOMOAI_RESIDENT_ORIGIN", "https://home.example")
    with pytest.raises(ValueError, match="MCP path"):
        Settings.from_environment()


def test_t012_b0_reserved_path_neighbors_remain_valid() -> None:
    for path in ("/api/v1/resident", "/api/v1/resident/session/x", "/api/v1/resident/sessions"):
        assert Settings(mcp_path=path).mcp_path == path
