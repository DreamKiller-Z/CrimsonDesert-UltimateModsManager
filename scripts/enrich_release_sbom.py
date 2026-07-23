"""Add non-Python vendored binaries to the generated release SBOM."""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path


ATTEST_SBOM_ACTION_COMMIT = "4651f806c01d8637787e274ac3bdf724ef169f34"
SBOM_SERIAL_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    (
        "https://github.com/DreamKiller-Z/"
        "CrimsonDesert-UltimateModsManager#sublate.cdumm.v1"
    ),
)


@dataclass(frozen=True)
class VendoredComponent:
    """Describe one repository binary omitted by environment-only SBOM tools.

    CycloneDX's Python environment scanner sees installed distributions, but
    it cannot discover binaries copied directly by the PyInstaller spec.  Each
    instance records the binary's exact repository path plus the strongest
    available source identity.  Unknown source mappings are represented
    explicitly in ``provenance_status`` rather than inferred from a filename.
    """

    name: str
    version: str
    relative_path: str
    license_id: str
    source_url: str
    source_archive_sha256: str | None
    source_member_name: str | None
    provenance_status: str
    provenance_note: str

    def to_cyclonedx(self, repository_root: Path) -> dict:
        """Return a CycloneDX component with hash, size, and provenance.

        The component is intentionally described as a file rather than a
        package when an exact source-to-binary mapping cannot be proven.  This
        prevents downstream tooling from mistaking a repository URL for a
        reproducible binary build identity.
        """
        binary_path = repository_root / self.relative_path
        payload = binary_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        properties = [
            {"name": "sublate:repository-path", "value": self.relative_path},
            {"name": "sublate:size-bytes", "value": str(len(payload))},
            {
                "name": "sublate:provenance-status",
                "value": self.provenance_status,
            },
            {"name": "sublate:provenance-note", "value": self.provenance_note},
        ]
        if self.source_archive_sha256 is not None:
            properties.append({
                "name": "sublate:source-archive-sha256",
                "value": self.source_archive_sha256,
            })
        if self.source_member_name is not None:
            properties.append({
                "name": "sublate:source-member-name",
                "value": self.source_member_name,
            })
        return {
            "bom-ref": f"vendored-file:sha256:{digest}",
            "type": "file",
            "name": self.name,
            "version": self.version,
            "hashes": [{"alg": "SHA-256", "content": digest}],
            "licenses": [{"license": {"id": self.license_id}}],
            "externalReferences": [{
                "type": "distribution",
                "url": self.source_url,
            }],
            "properties": properties,
        }


VENDORED_COMPONENTS = (
    VendoredComponent(
        name="crimson_rs.pyd",
        version="unknown-vendored-at-cdumm-3.6.0",
        relative_path="src/cdumm/_vendor/crimson_rs/crimson_rs.pyd",
        license_id="MPL-2.0",
        source_url="https://github.com/potter420/crimson-rs",
        source_archive_sha256=None,
        source_member_name=None,
        provenance_status="binary-hash-pinned-source-commit-unknown",
        provenance_note=(
            "CDUMM bundles LICENSE_MPL2, but the repository does not record "
            "the exact crimson-rs source commit or build that produced this "
            "978944-byte extension."
        ),
    ),
    VendoredComponent(
        name="Ultimate ASI Loader x64 (renamed winmm.dll)",
        version="9.7.1",
        relative_path="asi_loader/winmm.dll",
        license_id="MIT",
        source_url=(
            "https://github.com/ThirteenAG/Ultimate-ASI-Loader/releases/"
            "download/v9.7.1/Ultimate-ASI-Loader_x64.zip"
        ),
        source_archive_sha256=(
            "77da5b4c3ab4552b3ba605667961c9a46f1b6c78c80667d572d1e811e9670306"
        ),
        source_member_name="dinput8.dll",
        provenance_status="exact-release-asset-member-match",
        provenance_note=(
            "The vendored winmm.dll is byte-identical to dinput8.dll from "
            "the official v9.7.1 x64 release archive."
        ),
    ),
)


def serial_number_for_source(source_commit: str) -> str:
    """Derive one valid deterministic CycloneDX UUID from a Git commit.

    The release commit covers the dependency lock, build specification,
    protocol implementation, and vendored binary hashes. UUIDv5 converts that
    immutable identity into the ``urn:uuid:`` form required by CycloneDX while
    producing the same serial across dry-run and tag builds of the same source.
    """

    if len(source_commit) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise ValueError("source commit must be lowercase hexadecimal")
    serial = uuid.uuid5(
        SBOM_SERIAL_NAMESPACE,
        f"sublate.cdumm.v1:git-commit:{source_commit}",
    )
    return f"urn:uuid:{serial}"


def validate_attest_sbom_compatibility(document: dict) -> None:
    """Enforce the exact CycloneDX discriminator used by the pinned action.

    ``actions/attest-sbom`` at ``ATTEST_SBOM_ACTION_COMMIT`` accepts CycloneDX
    only when ``bomFormat``, ``serialNumber``, and ``specVersion`` are truthy.
    Keeping that parser contract here makes a missing reproducible-generator
    field a release-blocking error before the attestation step is reached.
    """

    if not (
        document.get("bomFormat")
        and document.get("serialNumber")
        and document.get("specVersion")
    ):
        raise ValueError(
            "SBOM is incompatible with the pinned attest-sbom CycloneDX parser"
        )


def enrich_sbom(
    sbom_path: Path,
    repository_root: Path,
    source_commit: str,
) -> None:
    """Add serial/provenance data and rewrite the SBOM deterministically."""

    document = json.loads(sbom_path.read_text(encoding="utf-8"))
    document["serialNumber"] = serial_number_for_source(source_commit)
    components = document.setdefault("components", [])
    components.extend(
        component.to_cyclonedx(repository_root)
        for component in VENDORED_COMPONENTS
    )
    components.sort(key=lambda component: component.get("bom-ref", ""))
    validate_attest_sbom_compatibility(document)
    sbom_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Parse release paths and enrich the CycloneDX document in place."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    enrich_sbom(args.input, args.repository_root, args.source_commit)


if __name__ == "__main__":
    main()
