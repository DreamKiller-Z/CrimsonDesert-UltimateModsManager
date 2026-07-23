"""Create and verify a source ZIP from exact Git blob object bytes.

Git for Windows can apply checkout line-ending conversion while producing a
ZIP archive.  A release source archive is provenance evidence, so every member
must instead be byte-identical to the blob named by the release commit.  This
module reads blobs through ``git cat-file --batch`` and independently verifies
the completed ZIP against the same immutable tree.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class SourceArchiveError(RuntimeError):
    """Report a closed failure to create or verify exact source evidence.

    Release automation must stop rather than publish an archive whose members,
    paths, Git modes, or bytes cannot be proven against the selected commit.
    Messages intentionally identify only repository-relative members so build
    machine paths do not leak into durable evidence or workflow summaries.
    """


@dataclass(frozen=True)
class TreeBlob:
    """Describe one immutable blob entry in a recursively listed Git tree.

    ``mode`` retains Git's six-digit tree mode, ``object_id`` identifies the
    exact blob, and ``path`` is the validated repository-relative POSIX path
    used as the ZIP member suffix.
    """

    mode: str
    object_id: str
    path: str


@dataclass(frozen=True)
class ArchiveSummary:
    """Return the stable identity facts emitted by create and verify commands.

    The resolved commit and entry count let workflow logs prove what was
    processed without including a runner-specific absolute archive path.
    """

    commit: str
    entry_count: int
    outcome: str

    def as_json(self) -> str:
        """Serialize the summary as deterministic, path-free JSON."""

        return json.dumps(
            {
                "commit": self.commit,
                "entry_count": self.entry_count,
                "outcome": self.outcome,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def _run_git(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
) -> bytes:
    """Run one Git plumbing command and return its unmodified stdout bytes.

    Binary pipes are mandatory because decoded text streams could perform
    newline or encoding conversion.  Failure output is deliberately reduced
    to the command and exit code; Git's raw stderr may contain a host path.
    """

    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        command = " ".join(arguments[:2])
        raise SourceArchiveError(
            f"git {command} failed with exit code {result.returncode}"
        )
    return result.stdout


def resolve_commit(repository: Path, revision: str) -> str:
    """Resolve ``revision`` to the immutable commit object it ultimately names."""

    resolved = _run_git(
        repository,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
    ).strip()
    try:
        commit = resolved.decode("ascii")
    except UnicodeDecodeError as error:
        raise SourceArchiveError("resolved commit id is not ASCII") from error
    if not commit or any(character not in "0123456789abcdef" for character in commit):
        raise SourceArchiveError("resolved commit id is not lowercase hexadecimal")
    return commit


def _validate_tree_path(raw_path: bytes) -> str:
    """Decode and reject any tree path that is unsafe or ambiguous in a ZIP."""

    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceArchiveError("tree contains a non-UTF-8 path") from error

    parsed = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise SourceArchiveError(f"tree contains an unsafe path: {path!r}")
    return path


def list_tree_blobs(repository: Path, commit: str) -> list[TreeBlob]:
    """List every recursive tree entry and fail if one is not a Git blob.

    Submodule commit entries cannot be represented as blob-byte evidence
    without separately acquiring their repositories.  They are therefore a
    closed failure instead of being silently omitted from the source archive.
    """

    raw_listing = _run_git(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
    )
    entries: list[TreeBlob] = []
    for raw_entry in raw_listing.split(b"\0"):
        if not raw_entry:
            continue
        try:
            raw_metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
            raw_mode, raw_type, raw_object_id = raw_metadata.split(b" ", maxsplit=2)
        except ValueError as error:
            raise SourceArchiveError("git ls-tree returned an invalid entry") from error

        mode = raw_mode.decode("ascii")
        object_type = raw_type.decode("ascii")
        object_id = raw_object_id.decode("ascii")
        path = _validate_tree_path(raw_path)
        if object_type != "blob":
            raise SourceArchiveError(
                f"tree member is not a self-contained blob: {path}"
            )
        entries.append(TreeBlob(mode=mode, object_id=object_id, path=path))

    if len({entry.path for entry in entries}) != len(entries):
        raise SourceArchiveError("tree contains duplicate paths")
    return entries


def read_blob_payloads(
    repository: Path,
    entries: list[TreeBlob],
) -> list[bytes]:
    """Read every requested object through one binary ``cat-file`` batch.

    The batch framing is parsed by the declared byte size, never by newline
    scanning inside payloads, so arbitrary binaries and mixed line endings
    remain unchanged.
    """

    if not entries:
        return []

    query = b"".join(
        entry.object_id.encode("ascii") + b"\n" for entry in entries
    )
    response = _run_git(repository, "cat-file", "--batch", input_bytes=query)
    offset = 0
    payloads: list[bytes] = []

    for expected in entries:
        header_end = response.find(b"\n", offset)
        if header_end < 0:
            raise SourceArchiveError("git cat-file returned a truncated header")
        header = response[offset:header_end]
        offset = header_end + 1
        try:
            raw_object_id, raw_type, raw_size = header.split(b" ", maxsplit=2)
            object_id = raw_object_id.decode("ascii")
            object_type = raw_type.decode("ascii")
            size = int(raw_size.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SourceArchiveError("git cat-file returned an invalid header") from error

        payload_end = offset + size
        if payload_end >= len(response) or response[payload_end : payload_end + 1] != b"\n":
            raise SourceArchiveError("git cat-file returned a truncated blob")
        payload = response[offset:payload_end]
        offset = payload_end + 1

        if object_id != expected.object_id or object_type != "blob":
            raise SourceArchiveError(
                f"git cat-file returned the wrong object for {expected.path}"
            )
        payloads.append(payload)

    if offset != len(response):
        raise SourceArchiveError("git cat-file returned unexpected trailing bytes")
    return payloads


def normalize_prefix(prefix: str) -> str:
    """Return one safe, non-empty POSIX directory prefix ending in ``/``."""

    candidate = prefix.rstrip("/")
    parsed = PurePosixPath(candidate)
    if (
        not candidate
        or candidate.startswith("/")
        or "\\" in candidate
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise SourceArchiveError("archive prefix must be a safe relative directory")
    return f"{candidate}/"


def _zip_info(prefix: str, entry: TreeBlob) -> zipfile.ZipInfo:
    """Build deterministic ZIP metadata that retains the exact Git tree mode."""

    info = zipfile.ZipInfo(f"{prefix}{entry.path}", FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = int(entry.mode, 8) << 16
    return info


def create_source_archive(
    repository: Path,
    revision: str,
    output: Path,
    prefix: str,
) -> ArchiveSummary:
    """Create an atomic deterministic ZIP directly from immutable Git blobs."""

    commit = resolve_commit(repository, revision)
    normalized_prefix = normalize_prefix(prefix)
    entries = list_tree_blobs(repository, commit)
    payloads = read_blob_payloads(repository, entries)
    output.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for entry, payload in zip(entries, payloads, strict=True):
                archive.writestr(
                    _zip_info(normalized_prefix, entry),
                    payload,
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)

    return ArchiveSummary(
        commit=commit,
        entry_count=len(entries),
        outcome="created",
    )


def verify_source_archive(
    repository: Path,
    revision: str,
    archive_path: Path,
    prefix: str,
) -> ArchiveSummary:
    """Prove ZIP paths, modes, and payload bytes exactly match a Git tree."""

    commit = resolve_commit(repository, revision)
    normalized_prefix = normalize_prefix(prefix)
    entries = list_tree_blobs(repository, commit)
    payloads = read_blob_payloads(repository, entries)
    expected = {
        f"{normalized_prefix}{entry.path}": (entry, payload)
        for entry, payload in zip(entries, payloads, strict=True)
    }

    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos = archive.infolist()
            counts = Counter(info.filename for info in infos)
            duplicates = sorted(name for name, count in counts.items() if count != 1)
            if duplicates:
                raise SourceArchiveError(
                    f"archive contains duplicate member: {duplicates[0]}"
                )

            actual_names = set(counts)
            expected_names = set(expected)
            missing = sorted(expected_names - actual_names)
            extra = sorted(actual_names - expected_names)
            if missing:
                raise SourceArchiveError(f"archive is missing member: {missing[0]}")
            if extra:
                raise SourceArchiveError(f"archive contains extra member: {extra[0]}")

            for info in infos:
                entry, expected_payload = expected[info.filename]
                actual_mode = (info.external_attr >> 16) & 0xFFFF
                if actual_mode != int(entry.mode, 8):
                    raise SourceArchiveError(
                        f"archive mode differs from Git tree: {info.filename}"
                    )
                if archive.read(info) != expected_payload:
                    raise SourceArchiveError(
                        f"archive payload differs from Git blob: {info.filename}"
                    )
    except (OSError, zipfile.BadZipFile) as error:
        raise SourceArchiveError("source archive is unreadable") from error

    return ArchiveSummary(
        commit=commit,
        entry_count=len(entries),
        outcome="verified",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Define the closed create/verify command surface used by CI."""

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--repository", type=Path, required=True)
        subparser.add_argument("--commit", required=True)
        subparser.add_argument("--archive", type=Path, required=True)
        subparser.add_argument("--prefix", required=True)
    return parser


def main() -> None:
    """Create or verify an archive and emit only a path-free JSON summary."""

    args = _build_parser().parse_args()
    try:
        if args.command == "create":
            summary = create_source_archive(
                args.repository,
                args.commit,
                args.archive,
                args.prefix,
            )
        else:
            summary = verify_source_archive(
                args.repository,
                args.commit,
                args.archive,
                args.prefix,
            )
    except SourceArchiveError as error:
        print(
            json.dumps(
                {"error": str(error), "outcome": "failed"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(summary.as_json())


if __name__ == "__main__":
    main()
