"""Tests for byte-preserving upstream patch release evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.source_tree_patch import (
    SourcePatchError,
    create_upstream_patch,
    verify_upstream_patch,
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


def _create_repository(tmp_path: Path) -> tuple[Path, str, str]:
    """Create base/source commits with text, binary, deletion, and mode changes."""

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Source Patch Test")
    _git(repository, "config", "user.email", "source-patch@example.invalid")
    _git(repository, "config", "core.autocrlf", "false")

    (repository / "tracked.txt").write_bytes(b"base\ntext\n")
    (repository / "binary.dat").write_bytes(b"\x00base\xff")
    (repository / "deleted.txt").write_bytes(b"delete me\n")
    (repository / "mode.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "base")
    base_commit = _git(repository, "rev-parse", "HEAD").strip().decode("ascii")

    (repository / "tracked.txt").write_bytes(b"source\ntext\n")
    (repository / "binary.dat").write_bytes(b"\x00source\xfe\xff")
    (repository / "deleted.txt").unlink()
    (repository / "added.txt").write_bytes(b"added\n")
    _git(repository, "add", "--all")
    _git(repository, "update-index", "--chmod=+x", "mode.sh")
    _git(repository, "commit", "--quiet", "-m", "source")
    source_commit = _git(repository, "rev-parse", "HEAD").strip().decode("ascii")
    return repository, base_commit, source_commit


def test_patch_reproduces_exact_source_tree(tmp_path: Path) -> None:
    """The isolated-index gate proves all text, binary, and mode semantics."""

    repository, base_commit, source_commit = _create_repository(tmp_path)
    patch_path = tmp_path / "upstream.patch"

    created = create_upstream_patch(
        repository,
        base_commit,
        source_commit,
        patch_path,
    )
    verified = verify_upstream_patch(
        repository,
        base_commit,
        source_commit,
        patch_path,
    )

    expected_tree = (
        _git(repository, "rev-parse", f"{source_commit}^{{tree}}")
        .strip()
        .decode("ascii")
    )
    assert b"GIT binary patch" in patch_path.read_bytes()
    assert created.source_tree == expected_tree
    assert created.outcome == "created"
    assert verified.source_tree == expected_tree
    assert verified.outcome == "verified"


def test_verifier_rejects_crlf_rewritten_patch(tmp_path: Path) -> None:
    """PowerShell-style LF-to-CRLF rewriting fails before publication."""

    repository, base_commit, source_commit = _create_repository(tmp_path)
    patch_path = tmp_path / "upstream.patch"
    rewritten_path = tmp_path / "rewritten.patch"
    create_upstream_patch(repository, base_commit, source_commit, patch_path)
    rewritten_path.write_bytes(patch_path.read_bytes().replace(b"\n", b"\r\n"))

    with pytest.raises(SourcePatchError, match="git apply --cached failed"):
        verify_upstream_patch(
            repository,
            base_commit,
            source_commit,
            rewritten_path,
        )
