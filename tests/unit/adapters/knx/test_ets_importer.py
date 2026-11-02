from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from domoai.adapters.knx.ets import EtsImporter, EtsImportError


def _ets_xml() -> bytes:
    return b"""<?xml version='1.0'?>
<Project name='Casa Demo'>
  <Area name='Planta Baja'>
    <GroupAddress address='1/0/1' dpt='1.001' name='Luz salon'/>
    <GroupAddress address='1/0/2' dpt='5.001' name='Luz salon'/>
    <GroupAddress address='2/1/1' dpt='9.001' name='Temperatura salon'/>
    <GroupAddress address='2/1/2' dpt='20.001' name='Modo propietario'/>
  </Area>
</Project>
"""


def test_importer_extracts_metadata_addresses_and_supported_mapping(tmp_path: Path) -> None:
    source = tmp_path / "project.xml"
    source.write_bytes(_ets_xml())

    preview = EtsImporter().preview(source)

    assert preview.project_name == "Casa Demo"
    assert [item.address for item in preview.group_addresses] == [
        "1/0/1",
        "1/0/2",
        "2/1/1",
        "2/1/2",
    ]
    assert preview.group_addresses[-1].support == "unsupported"
    assert preview.mapping is not None
    assert len(preview.mapping.entities) == 2
    assert {item.name for item in preview.mapping.entities} == {"Luz salon", "Temperatura salon"}
    assert preview.source_digest and len(preview.source_digest) == 64


def test_importer_reads_knxproj_zip_and_produces_same_preview(tmp_path: Path) -> None:
    source = tmp_path / "project.knxproj"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project.xml", _ets_xml())

    preview = EtsImporter().preview(source)

    assert preview.source_kind == "knxproj"
    assert preview.project_name == "Casa Demo"
    assert preview.mapping is not None


def test_persist_requires_confirmation_and_exact_source_digest(tmp_path: Path) -> None:
    source = tmp_path / "project.xml"
    source.write_bytes(_ets_xml())
    target = tmp_path / "mapping.json"
    original = {"schema_version": "v1", "entities": []}
    target.write_text(json.dumps(original), encoding="utf-8")
    preview = EtsImporter().preview(source)

    with pytest.raises(EtsImportError, match="confirmation"):
        EtsImporter().persist(preview, source_path=source, target_path=target, confirmed=False)
    assert json.loads(target.read_text(encoding="utf-8")) == original

    source.write_bytes(_ets_xml().replace(b"Casa Demo", b"Casa Cambiada"))
    with pytest.raises(EtsImportError, match="digest"):
        EtsImporter().persist(preview, source_path=source, target_path=target, confirmed=True)
    assert json.loads(target.read_text(encoding="utf-8")) == original


def test_persist_atomically_writes_reviewed_mapping(tmp_path: Path) -> None:
    source = tmp_path / "project.xml"
    source.write_bytes(_ets_xml())
    target = tmp_path / "mapping.json"
    preview = EtsImporter().preview(source)

    result = EtsImporter().persist(preview, source_path=source, target_path=target, confirmed=True)

    assert result == target
    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == "v1"
    assert len(persisted["entities"]) == 2
