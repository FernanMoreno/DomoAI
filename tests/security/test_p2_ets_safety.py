from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from domoai.adapters.knx.ets import EtsImporter, EtsImportError


def test_importer_rejects_external_entity_declarations(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.xml"
    source.write_text(
        "<!DOCTYPE Project [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>"
        "<Project name='unsafe'><GroupAddress address='1/0/1' dpt='1.001' name='&xxe;'/></Project>",
        encoding="utf-8",
    )

    with pytest.raises(EtsImportError, match="XML"):
        EtsImporter().preview(source)


def test_importer_rejects_archive_with_too_many_members(tmp_path: Path) -> None:
    source = tmp_path / "too-many.knxproj"
    with zipfile.ZipFile(source, "w") as archive:
        for index in range(257):
            archive.writestr(f"member-{index}.xml", b"<Project name='x'/>")

    with pytest.raises(EtsImportError, match="entries"):
        EtsImporter().preview(source)


def test_importer_rejects_empty_or_malformed_source(tmp_path: Path) -> None:
    source = tmp_path / "empty.xml"
    source.write_bytes(b"<Project name='x'/>")

    with pytest.raises(EtsImportError, match="group"):
        EtsImporter().preview(source)
