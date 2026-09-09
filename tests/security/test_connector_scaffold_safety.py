from __future__ import annotations

import pytest

from domoai.admin.connector_scaffold import ConnectorScaffoldRequest, create_connector_scaffold


@pytest.mark.parametrize(
    "connector_id",
    ["../escape", "UPPER", "connector/child", "connector\\child", ""],
)
def test_scaffold_rejects_unsafe_connector_ids(tmp_path, connector_id: str) -> None:
    with pytest.raises(ValueError):
        create_connector_scaffold(
            ConnectorScaffoldRequest(connector_id, "Safe", "protocol", tmp_path / "out")
        )


def test_scaffold_refuses_non_empty_destination(tmp_path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="new empty directory"):
        create_connector_scaffold(
            ConnectorScaffoldRequest("safe_connector", "Safe", "protocol", output)
        )
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_scaffold_refuses_symlink_destination(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError):
        create_connector_scaffold(
            ConnectorScaffoldRequest("safe_connector", "Safe", "protocol", link)
        )
    assert not (target / "pyproject.toml").exists()


@pytest.mark.parametrize("value", ["access-token-protocol", "secret-protocol", "password-service"])
def test_scaffold_does_not_put_secret_words_in_provider_metadata(tmp_path, value: str) -> None:
    with pytest.raises(ValueError, match="sensitive"):
        create_connector_scaffold(
            ConnectorScaffoldRequest("safe_connector", "Safe", value, tmp_path / "out")
        )

