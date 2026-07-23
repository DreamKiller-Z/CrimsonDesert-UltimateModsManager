"""Exercise the frozen CDUMM protocol through real Windows process pipes."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from cdumm.storage.database import Database


class PackagedProtocolProbe:
    """Create a synthetic manager and verify the packaged lifecycle contract.

    Source-level tests cannot prove that a ``console=False`` PyInstaller
    executable preserves redirected stdout.  This probe launches the frozen
    executable exactly as an external protocol host does and verifies version,
    help, inventory, removal, retry, rejection, and unrelated-state behavior.
    """

    def __init__(self, executable: Path) -> None:
        """Remember one exact executable and allocate no persistent state."""
        self.executable = executable.resolve()

    def _run(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        """Run the executable with captured UTF-8 streams and a hard timeout."""
        return subprocess.run(
            [str(self.executable), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
            timeout=30,
        )

    def run(self) -> None:
        """Execute all packaged contract checks in a disposable sandbox."""
        with tempfile.TemporaryDirectory(prefix="cdumm-protocol-probe-") as raw:
            sandbox = Path(raw)
            game_dir = sandbox / "CrimsonDesert"
            cdmods_dir = game_dir / "CDMods"
            cdmods_dir.mkdir(parents=True)
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(sandbox / "LocalAppData")

            db = Database(cdmods_dir / "cdumm.db")
            db.initialize()
            db.connection.executemany(
                "INSERT INTO mods "
                "(id, name, mod_type, enabled, priority) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (1, "Removal target", "paz", 0, 10),
                    (2, "Unrelated", "paz", 0, 20),
                    (3, "Enabled", "asi", 1, 30),
                ),
            )
            db.connection.commit()
            db.close()
            for mod_id in (1, 2, 3):
                delta_dir = cdmods_dir / "deltas" / str(mod_id)
                source_dir = cdmods_dir / "sources" / str(mod_id)
                delta_dir.mkdir(parents=True)
                source_dir.mkdir(parents=True)
                (delta_dir / "payload.bin").write_bytes(
                    f"delta-{mod_id}".encode()
                )
                (source_dir / "source.bin").write_bytes(
                    f"source-{mod_id}".encode()
                )

            version = self._run(["--version"], environment=environment)
            assert version.returncode == 0, version.stderr
            assert version.stdout.strip() == (
                "CDUMM 3.6.0+sub432.1 (sublate.cdumm.v1)"
            )

            for command, required_flags in (
                ("list-mods", ("--read-only", "--json", "--game-dir")),
                ("remove-mod", ("--mod-id", "--game-dir")),
            ):
                help_result = self._run(
                    [command, "--help"],
                    environment=environment,
                )
                assert help_result.returncode == 0, help_result.stderr
                for flag in required_flags:
                    assert flag in help_result.stdout

            inventory = self._run(
                [
                    "list-mods",
                    "--read-only",
                    "--json",
                    "--game-dir",
                    str(game_dir),
                ],
                environment=environment,
            )
            assert inventory.returncode == 0, inventory.stderr
            assert [row["id"] for row in json.loads(inventory.stdout)] == [
                1,
                2,
                3,
            ]

            enabled = self._run(
                [
                    "remove-mod",
                    "--mod-id",
                    "3",
                    "--game-dir",
                    str(game_dir),
                ],
                environment=environment,
            )
            assert enabled.returncode != 0
            assert enabled.stdout == ""
            assert json.loads(enabled.stderr)["error"]["code"] == "mod_enabled"

            removed = self._run(
                [
                    "remove-mod",
                    "--mod-id",
                    "1",
                    "--game-dir",
                    str(game_dir),
                ],
                environment=environment,
            )
            assert removed.returncode == 0, removed.stderr
            assert json.loads(removed.stdout) == {
                "id": 1,
                "outcome": "removed",
            }

            retry = self._run(
                [
                    "remove-mod",
                    "--mod-id",
                    "1",
                    "--game-dir",
                    str(game_dir),
                ],
                environment=environment,
            )
            assert retry.returncode == 0, retry.stderr
            assert json.loads(retry.stdout) == {
                "id": 1,
                "outcome": "already_absent",
            }

            connection = sqlite3.connect(cdmods_dir / "cdumm.db")
            try:
                remaining = connection.execute(
                    "SELECT id FROM mods ORDER BY id"
                ).fetchall()
            finally:
                connection.close()
            assert remaining == [(2,), (3,)]
            assert (cdmods_dir / "deltas" / "2" / "payload.bin").is_file()
            assert (cdmods_dir / "sources" / "2" / "source.bin").is_file()


def main() -> None:
    """Parse the frozen executable path and run the complete probe."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    PackagedProtocolProbe(args.executable).run()


if __name__ == "__main__":
    main()
