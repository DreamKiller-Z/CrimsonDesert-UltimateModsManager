"""Tests for byte-exact Git tree source archive evidence."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.source_tree_archive import (
    SourceArchiveError,
    create_source_archive,
    verify_source_archive,
)


def _git(repository: Path, *arguments: str) -> bytes:
    """Run Git in the synthetic repository and return raw stdout bytes."""

    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _create_repository(tmp_path: Path) -> tuple[Path, str, bytes]:
    """Create a commit whose LF blob later differs from its working-tree file."""

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Source Archive Test")
    _git(repository, "config", "user.email", "source-archive@example.invalid")
    tracked_payload = b"alpha\nbeta\n"
    tracked_path = repository / "tracked.txt"
    tracked_path.write_bytes(tracked_payload)
    binary_path = repository / "binary.dat"
    binary_path.write_bytes(b"\x00\xff\n\r\n\x7f")
    _git(repository, "add", "tracked.txt", "binary.dat")
    _git(repository, "commit", "--quiet", "-m", "fixture")
    commit = _git(repository, "rev-parse", "HEAD").strip().decode("ascii")

    # The archive must come from Git objects, even if checkout conversion or
    # unrelated local edits make the working tree contain different bytes.
    tracked_path.write_bytes(b"alpha\r\nbeta\r\n")
    return repository, commit, tracked_payload


def test_archive_members_are_exact_git_blob_bytes(tmp_path: Path) -> None:
    """Creation ignores CRLF working bytes and verification proves tree parity."""

    repository, commit, tracked_payload = _create_repository(tmp_path)
    archive_path = tmp_path / "source.zip"

    created = create_source_archive(
        repository,
        commit,
        archive_path,
        "CDUMM-test/",
    )
    verified = verify_source_archive(
        repository,
        commit,
        archive_path,
        "CDUMM-test/",
    )

    assert created.commit == commit
    assert created.entry_count == 2
    assert created.outcome == "created"
    assert verified.commit == commit
    assert verified.entry_count == 2
    assert verified.outcome == "verified"
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        assert archive.read("CDUMM-test/tracked.txt") == tracked_payload
        assert archive.read("CDUMM-test/binary.dat") == b"\x00\xff\n\r\n\x7f"


def test_verifier_rejects_line_ending_rewrite(tmp_path: Path) -> None:
    """A ZIP member rewritten from LF to CRLF fails the release gate."""

    repository, commit, _ = _create_repository(tmp_path)
    archive_path = tmp_path / "source.zip"
    rewritten_path = tmp_path / "rewritten.zip"
    create_source_archive(repository, commit, archive_path, "CDUMM-test/")

    with (
        zipfile.ZipFile(archive_path, mode="r") as source,
        zipfile.ZipFile(rewritten_path, mode="w") as rewritten,
    ):
        for info in source.infolist():
            payload = source.read(info)
            if info.filename.endswith("/tracked.txt"):
                payload = payload.replace(b"\n", b"\r\n")
            rewritten.writestr(info, payload)

    with pytest.raises(SourceArchiveError, match="payload differs from Git blob"):
        verify_source_archive(
            repository,
            commit,
            rewritten_path,
            "CDUMM-test/",
        )
