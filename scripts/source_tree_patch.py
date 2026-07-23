"""Create and verify a byte-preserving patch between two exact Git trees.

PowerShell pipelines decode native stdout as text and can rewrite LF patch
records to CRLF.  That corrupts provenance evidence and can break binary patch
application.  This module makes Git write the patch file directly, then applies
that exact file to an isolated index loaded from the upstream base.  The
resulting tree object must equal the intended source tree before release.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SourcePatchError(RuntimeError):
    """Report a closed failure in patch creation or tree reproduction.

    Errors intentionally omit raw Git stderr because it can contain
    runner-specific paths.  A failed operation is enough to block publication;
    local debugging can rerun the named plumbing command when necessary.
    """


@dataclass(frozen=True)
class PatchSummary:
    """Describe the immutable base, source, and verified result tree."""

    base_commit: str
    source_commit: str
    source_tree: str
    outcome: str

    def as_json(self) -> str:
        """Serialize stable patch evidence without an absolute host path."""

        return json.dumps(
            {
                "base_commit": self.base_commit,
                "outcome": self.outcome,
                "source_commit": self.source_commit,
                "source_tree": self.source_tree,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def _run_git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> bytes:
    """Run one Git plumbing command with binary stdout and closed errors."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        command = " ".join(arguments[:2])
        raise SourcePatchError(
            f"git {command} failed with exit code {result.returncode}"
        )
    return result.stdout


def _resolve_object(
    repository: Path,
    revision: str,
    object_type: str,
) -> str:
    """Resolve a revision to a lowercase hexadecimal commit or tree id."""

    resolved = _run_git(
        repository,
        "rev-parse",
        "--verify",
        f"{revision}^{{{object_type}}}",
    ).strip()
    try:
        object_id = resolved.decode("ascii")
    except UnicodeDecodeError as error:
        raise SourcePatchError(f"resolved {object_type} id is not ASCII") from error
    if not object_id or any(
        character not in "0123456789abcdef" for character in object_id
    ):
        raise SourcePatchError(
            f"resolved {object_type} id is not lowercase hexadecimal"
        )
    return object_id


def create_upstream_patch(
    repository: Path,
    base_revision: str,
    source_revision: str,
    output: Path,
) -> PatchSummary:
    """Have Git write an atomic full-index binary patch without a text shell.

    Fixed diff options exclude user-configured rename, text-conversion, color,
    and external-diff behavior.  ``--output`` makes Git write raw bytes directly
    instead of crossing PowerShell's native-command text pipeline.
    """

    base_commit = _resolve_object(repository, base_revision, "commit")
    source_commit = _resolve_object(repository, source_revision, "commit")
    source_tree = _resolve_object(repository, source_commit, "tree")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _run_git(
            repository,
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            f"--output={temporary_path.resolve()}",
            base_commit,
            source_commit,
            "--",
        )
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)

    return PatchSummary(
        base_commit=base_commit,
        source_commit=source_commit,
        source_tree=source_tree,
        outcome="created",
    )


def verify_upstream_patch(
    repository: Path,
    base_revision: str,
    source_revision: str,
    patch_path: Path,
) -> PatchSummary:
    """Apply the exact patch to an isolated base index and compare tree ids.

    ``git read-tree`` seeds a temporary index with the upstream base without
    touching the caller's index or worktree.  ``git apply --cached --binary``
    exercises text hunks, binary literals/deltas, deletions, additions, and
    mode changes.  ``git write-tree`` then provides one exact equality check
    across every path, mode, and resulting blob.
    """

    base_commit = _resolve_object(repository, base_revision, "commit")
    source_commit = _resolve_object(repository, source_revision, "commit")
    expected_tree = _resolve_object(repository, source_commit, "tree")
    resolved_patch = patch_path.resolve()
    if not resolved_patch.is_file():
        raise SourcePatchError("upstream patch is missing")

    with tempfile.TemporaryDirectory(prefix="cdumm-patch-index-") as directory:
        index_path = Path(directory) / "index"
        isolated_environment = os.environ.copy()
        isolated_environment["GIT_INDEX_FILE"] = str(index_path)
        _run_git(
            repository,
            "read-tree",
            base_commit,
            environment=isolated_environment,
        )
        _run_git(
            repository,
            "apply",
            "--cached",
            "--binary",
            "--check",
            str(resolved_patch),
            environment=isolated_environment,
        )
        _run_git(
            repository,
            "apply",
            "--cached",
            "--binary",
            str(resolved_patch),
            environment=isolated_environment,
        )
        actual_tree = (
            _run_git(
                repository,
                "write-tree",
                environment=isolated_environment,
            )
            .strip()
            .decode("ascii")
        )

    if actual_tree != expected_tree:
        raise SourcePatchError(
            "applied upstream patch tree does not equal the source tree"
        )
    return PatchSummary(
        base_commit=base_commit,
        source_commit=source_commit,
        source_tree=expected_tree,
        outcome="verified",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Define the bounded create/verify command surface used by CI."""

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--repository", type=Path, required=True)
        subparser.add_argument("--base", required=True)
        subparser.add_argument("--source", required=True)
        subparser.add_argument("--patch", type=Path, required=True)
    return parser


def main() -> None:
    """Create or verify a patch and emit a path-free JSON result."""

    args = _build_parser().parse_args()
    try:
        if args.command == "create":
            summary = create_upstream_patch(
                args.repository,
                args.base,
                args.source,
                args.patch,
            )
        else:
            summary = verify_upstream_patch(
                args.repository,
                args.base,
                args.source,
                args.patch,
            )
    except SourcePatchError as error:
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
