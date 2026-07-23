"""Tests for non-Python vendored component release evidence."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from scripts.enrich_release_sbom import (
    ATTEST_SBOM_ACTION_COMMIT,
    VENDORED_COMPONENTS,
    enrich_sbom,
    serial_number_for_source,
    validate_attest_sbom_compatibility,
)


FIXTURE_SOURCE_COMMIT = "1234567890abcdef1234567890abcdef12345678"


def test_vendored_component_hashes_and_provenance_are_explicit() -> None:
    """Pinned metadata matches repository bytes and never invents provenance."""
    repository_root = Path(__file__).parents[1]
    by_name = {component.name: component for component in VENDORED_COMPONENTS}

    crimson = by_name["crimson_rs.pyd"]
    crimson_bytes = (repository_root / crimson.relative_path).read_bytes()
    assert hashlib.sha256(crimson_bytes).hexdigest() == (
        "15890f77991d4844e758f0ac70ecdf1f949df3392eea010c7157497e654cfaa0"
    )
    assert crimson.source_archive_sha256 is None
    assert crimson.provenance_status.endswith("source-commit-unknown")

    asi = by_name["Ultimate ASI Loader x64 (renamed winmm.dll)"]
    asi_bytes = (repository_root / asi.relative_path).read_bytes()
    assert hashlib.sha256(asi_bytes).hexdigest() == (
        "810111a7f6a6cef892877c9f7c4582ccde2d621d119891f700c5309c370508bf"
    )
    assert asi.source_archive_sha256 == (
        "77da5b4c3ab4552b3ba605667961c9a46f1b6c78c80667d572d1e811e9670306"
    )
    assert asi.source_member_name == "dinput8.dll"


def test_enrichment_is_path_free_and_includes_both_binary_components(
    tmp_path: Path,
) -> None:
    """Generated SBOM exposes hashes and relative paths without host paths."""
    repository_root = Path(__file__).parents[1]
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(
        json.dumps({
            "bomFormat": "CycloneDX",
            "components": [],
            "specVersion": "1.6",
        }),
        encoding="utf-8",
    )

    enrich_sbom(sbom_path, repository_root, FIXTURE_SOURCE_COMMIT)

    rendered = sbom_path.read_text(encoding="utf-8")
    document = json.loads(rendered)
    names = {component["name"] for component in document["components"]}
    assert "crimson_rs.pyd" in names
    assert "Ultimate ASI Loader x64 (renamed winmm.dll)" in names
    assert str(repository_root) not in rendered


def test_sbom_serial_is_deterministic_valid_and_source_derived() -> None:
    """UUIDv5 serials are stable per commit and distinct across source commits."""

    serial = serial_number_for_source(FIXTURE_SOURCE_COMMIT)
    repeated = serial_number_for_source(FIXTURE_SOURCE_COMMIT)
    different = serial_number_for_source("a" * 40)

    assert serial == repeated
    assert serial != different
    assert serial.startswith("urn:uuid:")
    parsed = uuid.UUID(serial.removeprefix("urn:uuid:"))
    assert parsed.version == 5
    assert str(parsed) == serial.removeprefix("urn:uuid:")


def test_enriched_sbom_matches_exact_pinned_attest_parser(tmp_path: Path) -> None:
    """Generated evidence satisfies the parser at the workflow's exact pin."""

    repository_root = Path(__file__).parents[1]
    workflow = (
        repository_root / ".github/workflows/sublate-protocol-release.yml"
    ).read_text(encoding="utf-8")
    assert f"actions/attest-sbom@{ATTEST_SBOM_ACTION_COMMIT}" in workflow

    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(
        json.dumps({
            "bomFormat": "CycloneDX",
            "components": [],
            "specVersion": "1.6",
        }),
        encoding="utf-8",
    )
    enrich_sbom(sbom_path, repository_root, FIXTURE_SOURCE_COMMIT)
    document = json.loads(sbom_path.read_text(encoding="utf-8"))

    # This is the exact truthy-field discriminator in
    # actions/attest-sbom/src/sbom.ts::checkIsCycloneDX at the pinned commit.
    pinned_parser_accepts = bool(
        document.get("bomFormat")
        and document.get("serialNumber")
        and document.get("specVersion")
    )
    assert pinned_parser_accepts
    validate_attest_sbom_compatibility(document)
