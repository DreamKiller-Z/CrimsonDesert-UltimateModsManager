"""Tests for non-Python vendored component release evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.enrich_release_sbom import VENDORED_COMPONENTS, enrich_sbom


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
        json.dumps({"bomFormat": "CycloneDX", "components": []}),
        encoding="utf-8",
    )

    enrich_sbom(sbom_path, repository_root)

    rendered = sbom_path.read_text(encoding="utf-8")
    document = json.loads(rendered)
    names = {component["name"] for component in document["components"]}
    assert "crimson_rs.pyd" in names
    assert "Ultimate ASI Loader x64 (renamed winmm.dll)" in names
    assert str(repository_root) not in rendered
