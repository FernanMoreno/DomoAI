"""Bounded, review-gated import of ETS XML and ``.knxproj`` exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

from domoai.adapters.knx.config import (
    CAPABILITY_DPTS,
    KnxCapabilityBinding,
    KnxCapabilityName,
    KnxEntityConfig,
    KnxMappingDocument,
    KnxSemanticType,
)
from domoai.domain.p2_connections import (
    EtsGroupAddress,
    EtsImportPreview,
    EtsImportWarning,
    EtsMappingDocument,
    EtsSourceKind,
    EtsSupportStatus,
)

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 256
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_GROUP_ADDRESSES = 1000

_ADDRESS = re.compile(r"^\d+/\d+(?:/\d+)?$")
_DPT = re.compile(r"(?<!\d)(\d{1,3})[.\-](\d{1,3})(?!\d)")
_SAFE_XML = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CAPABILITY_BY_DPT: dict[str, KnxCapabilityName] = {
    dpt: cast(KnxCapabilityName, capability) for capability, dpt in CAPABILITY_DPTS.items()
}


class EtsImportError(ValueError):
    """Safe operator-facing error for an invalid or untrusted ETS source."""


class EtsImporter:
    """Read an ETS export and create a deterministic mapping proposal."""

    def __init__(
        self,
        *,
        max_source_bytes: int = MAX_SOURCE_BYTES,
        max_archive_entries: int = MAX_ARCHIVE_ENTRIES,
        max_member_bytes: int = MAX_MEMBER_BYTES,
        max_group_addresses: int = MAX_GROUP_ADDRESSES,
    ) -> None:
        self.max_source_bytes = max_source_bytes
        self.max_archive_entries = max_archive_entries
        self.max_member_bytes = max_member_bytes
        self.max_group_addresses = max_group_addresses

    def preview(self, source_path: Path | str) -> EtsImportPreview:
        path = Path(source_path)
        source = self._read_source(path)
        digest = hashlib.sha256(source.raw_bytes).hexdigest()
        addresses, project_name = self._extract(source.xml_documents, path.stem)
        if not addresses:
            raise EtsImportError("ETS source contains no group addresses")
        mapping, warnings = self._build_mapping(addresses)
        return EtsImportPreview(
            source_digest=digest,
            source_kind=source.kind,
            project_name=project_name,
            group_addresses=tuple(addresses),
            mapping=mapping,
            warnings=tuple(warnings),
        )

    def persist(
        self,
        preview: EtsImportPreview,
        *,
        source_path: Path | str,
        target_path: Path | str,
        confirmed: bool,
    ) -> Path:
        if not confirmed:
            raise EtsImportError("explicit confirmation is required before ETS persistence")
        target = Path(target_path)
        source = Path(source_path)
        current = self._read_source(source)
        current_digest = hashlib.sha256(current.raw_bytes).hexdigest()
        if current_digest != preview.source_digest:
            raise EtsImportError("ETS source digest no longer matches the reviewed preview")
        if preview.mapping is None:
            raise EtsImportError("reviewed ETS preview has no supported mapping")
        if source.resolve() == target.resolve():
            raise EtsImportError("ETS source and mapping target must be different files")
        payload = json.dumps(
            preview.mapping.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temporary_path: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
        except OSError as error:
            raise EtsImportError("ETS mapping could not be persisted atomically") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return target

    def _read_source(self, path: Path) -> _EtsSource:
        try:
            raw = path.read_bytes()
        except (OSError, ValueError) as error:
            raise EtsImportError("ETS source could not be read") from error
        if not raw or len(raw) > self.max_source_bytes:
            raise EtsImportError("ETS source exceeds the configured size limit")
        if path.suffix.casefold() == ".knxproj" or zipfile.is_zipfile(path):
            return self._read_archive(path, raw)
        if _SAFE_XML.search(raw) is not None:
            raise EtsImportError("ETS XML contains a forbidden declaration")
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as error:
            raise EtsImportError("ETS XML is invalid") from error
        return _EtsSource(raw, EtsSourceKind.XML, (root,))

    def _read_archive(self, path: Path, raw: bytes) -> _EtsSource:
        documents: list[ElementTree.Element] = []
        total_uncompressed = 0
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > self.max_archive_entries:
                    raise EtsImportError("ETS archive contains too many entries")
                for info in sorted(infos, key=lambda item: item.filename):
                    name = info.filename.replace("\\", "/")
                    if info.is_dir() or not name.casefold().endswith(".xml"):
                        continue
                    if name.startswith("/") or any(part == ".." for part in name.split("/")):
                        raise EtsImportError("ETS archive contains an unsafe member path")
                    if info.flag_bits & 0x1 or info.file_size > self.max_member_bytes:
                        raise EtsImportError("ETS archive member exceeds the configured size limit")
                    total_uncompressed += info.file_size
                    if total_uncompressed > self.max_source_bytes:
                        raise EtsImportError("ETS archive exceeds the configured size limit")
                    member = archive.read(info)
                    if _SAFE_XML.search(member) is not None:
                        raise EtsImportError("ETS XML contains a forbidden declaration")
                    try:
                        documents.append(ElementTree.fromstring(member))
                    except ElementTree.ParseError:
                        continue
        except EtsImportError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            raise EtsImportError("ETS archive is invalid") from error
        if not documents:
            raise EtsImportError("ETS archive contains no readable XML")
        return _EtsSource(raw, EtsSourceKind.KNXPROJ, tuple(documents))

    def _extract(
        self, documents: Iterable[ElementTree.Element], fallback_name: str
    ) -> tuple[list[EtsGroupAddress], str]:
        extracted: dict[str, tuple[EtsGroupAddress, str]] = {}
        project_name = fallback_name or "ETS project"
        for root in documents:
            project_name = _first_text(root, ("project", "projectname"), "name") or _attribute(
                root, "name", "project_name", "projectname"
            ) or project_name
            for item, area in _walk_group_addresses(root):
                address = _address_from(item)
                dpt = _dpt_from(item)
                if address is None or dpt is None:
                    continue
                if not _valid_address(address):
                    raise EtsImportError("ETS contains an invalid group address")
                name = _attribute(item, "name", "displayname", "description") or _child_text(
                    item, "name", "description"
                ) or f"Group {address}"
                description = _attribute(item, "description", "comment") or _child_text(
                    item, "description", "comment"
                )
                group = EtsGroupAddress(
                    address=address,
                    dpt=dpt,
                    name=name,
                    area=area or _attribute(item, "area", "areaid"),
                    description=description,
                    support=(
                        EtsSupportStatus.SUPPORTED
                        if dpt in _CAPABILITY_BY_DPT
                        else EtsSupportStatus.UNSUPPORTED
                    ),
                    capability=_CAPABILITY_BY_DPT.get(dpt),
                )
                existing = extracted.get(address)
                if existing is not None and existing[0].dpt != dpt:
                    raise EtsImportError("ETS group address has conflicting DPT values")
                extracted.setdefault(address, (group, _element_key(item)))
                if len(extracted) > self.max_group_addresses:
                    raise EtsImportError("ETS contains too many group addresses")
        addresses = [
            item for item, _ in sorted(extracted.values(), key=lambda pair: pair[0].address)
        ]
        return addresses, project_name.strip()[:256] or fallback_name or "ETS project"

    @staticmethod
    def _build_mapping(
        addresses: Iterable[EtsGroupAddress],
    ) -> tuple[EtsMappingDocument | None, list[EtsImportWarning]]:
        grouped: dict[tuple[str, str, str], list[EtsGroupAddress]] = defaultdict(list)
        warnings: list[EtsImportWarning] = []
        for item in addresses:
            if item.support is not EtsSupportStatus.SUPPORTED or item.capability is None:
                warnings.append(
                    EtsImportWarning(
                        code="unsupported_dpt",
                        message=f"DPT {item.dpt} was not mapped automatically",
                        address=item.address,
                    )
                )
                continue
            semantic_type = _semantic_type(item.capability)
            grouped[(item.area or "unassigned", item.name, semantic_type)].append(item)
        entities: list[KnxEntityConfig] = []
        for (area, name, semantic_type_value), values in sorted(grouped.items()):
            semantic_type = cast(KnxSemanticType, semantic_type_value)
            capabilities = [
                KnxCapabilityBinding(
                    name=item.capability,
                    dpt=item.dpt,
                    state_group_address=item.address,
                )
                for item in sorted(values, key=lambda value: value.address)
                if item.capability is not None
            ]
            if not capabilities:
                continue
            area_id = _slug(area) if area != "unassigned" else None
            entity_id = f"ets-{_slug(area)}-{_slug(name)}"
            if any(existing.entity_id == entity_id for existing in entities):
                entity_id = f"{entity_id}-{_slug(values[0].address)}"
            entities.append(
                KnxEntityConfig(
                    entity_id=entity_id[:128],
                    name=name[:256],
                    area_id=area_id,
                    semantic_type=semantic_type,
                    manufacturer="ETS import",
                    model="review-required",
                    capabilities=capabilities,
                )
            )
        mapping = None
        if entities:
            canonical = KnxMappingDocument(schema_version="v1", entities=entities)
            mapping = EtsMappingDocument.model_validate(canonical.model_dump(mode="json"))
        return mapping, _unique_warnings(warnings)


class _EtsSource:
    def __init__(
        self,
        raw_bytes: bytes,
        kind: EtsSourceKind,
        xml_documents: tuple[ElementTree.Element, ...],
    ) -> None:
        self.raw_bytes = raw_bytes
        self.kind = kind
        self.xml_documents = xml_documents


def _walk_group_addresses(
    root: ElementTree.Element, area: str | None = None
) -> Iterable[tuple[ElementTree.Element, str | None]]:
    tag = _local_name(root.tag)
    current_area = area
    if tag in {"area", "room", "buildingpart", "floor"}:
        current_area = _attribute(root, "name", "id", "identifier") or area
    if tag in {"groupaddress", "group-address", "group_address", "ga"}:
        yield root, current_area
    for child in root:
        yield from _walk_group_addresses(child, current_area)


def _address_from(element: ElementTree.Element) -> str | None:
    value = _attribute(element, "address", "groupaddress", "group_address", "addressvalue")
    return value.strip() if value else _child_text(
        element, "address", "groupaddress", "group_address"
    )


def _dpt_from(element: ElementTree.Element) -> str | None:
    raw = _attribute(element, "dpt", "datapointtype", "datatype", "dptid") or _child_text(
        element, "dpt", "datapointtype", "datatype"
    )
    if raw is None:
        return None
    match = _DPT.search(raw.replace("_", "."))
    return f"{int(match.group(1))}.{int(match.group(2)):03d}" if match else None


def _first_text(
    root: ElementTree.Element, tags: tuple[str, ...], attribute: str
) -> str | None:
    for element in root.iter():
        if _local_name(element.tag) in tags:
            value = _attribute(element, attribute, "project_name", "projectname")
            if value:
                return value
    return None


def _child_text(element: ElementTree.Element, *tags: str) -> str | None:
    wanted = set(tags)
    for child in element:
        if _local_name(child.tag) in wanted and child.text:
            value = child.text.strip()
            if value:
                return value
    return None


def _attribute(element: ElementTree.Element, *names: str) -> str | None:
    normalized = {name.casefold().replace("-", "_") for name in names}
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1].casefold().replace("-", "_") in normalized:
            value = value.strip()
            if value:
                return value
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold().replace("_", "-")


def _element_key(element: ElementTree.Element) -> str:
    return _local_name(element.tag)


def _valid_address(address: str) -> bool:
    if _ADDRESS.fullmatch(address) is None:
        return False
    parts = [int(part) for part in address.split("/")]
    return (
        (len(parts) == 3 and 0 <= parts[0] <= 15 and 0 <= parts[1] <= 15 and 0 <= parts[2] <= 255)
        or (len(parts) == 2 and 0 <= parts[0] <= 31 and 0 <= parts[1] <= 2047)
    )


def _semantic_type(capability: str) -> KnxSemanticType:
    if capability in {"power", "brightness"}:
        return "light"
    if capability in {"battery.soc", "battery.power", "battery.capacity"}:
        return "energy"
    return "sensor"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "group"


def _unique_warnings(warnings: Iterable[EtsImportWarning]) -> list[EtsImportWarning]:
    seen: set[tuple[str, str | None]] = set()
    result: list[EtsImportWarning] = []
    for warning in warnings:
        key = (warning.code, warning.address)
        if key not in seen:
            result.append(warning)
            seen.add(key)
    return result


__all__ = ["EtsImportError", "EtsImporter"]
