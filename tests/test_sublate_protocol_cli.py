"""Contract tests for the provenance-pinned Sublate CDUMM CLI protocol.

The supported inventory path is explicitly read-only.  Removal is a separate,
bounded mutation which accepts only an existing, disabled manager row and must
converge safely when a filesystem interruption leaves a retryable partial
cleanup.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cdumm.storage.database import Database


def _tree_snapshot(root: Path) -> dict[str, str]:
    """Return hashes for durable files below ``root``.

    SQLite may create empty ``-wal`` and lock-coordination ``-shm`` sidecars
    when a read-only connection observes a database whose persisted journal
    mode is WAL.  Those transport files are not manager state, so this helper
    excludes them while still hashing the database and every mod-owned file.
    """
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.name.endswith(("-wal", "-shm"))
    }


def _seed_protocol_game(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Create a synthetic CDUMM manager with isolated pointer-file state."""
    game_dir = tmp_path / "CrimsonDesert"
    cdmods_dir = game_dir / "CDMods"
    cdmods_dir.mkdir(parents=True)

    # Prevent a developer's real cdmods_path pointer from redirecting this
    # synthetic protocol test outside its temporary sandbox.
    from cdumm.engine import cdmods_paths
    monkeypatch.setattr(
        cdmods_paths,
        "_APP_DATA_DIR",
        tmp_path / "isolated-app-data",
    )

    db = Database(cdmods_dir / "cdumm.db")
    db.initialize()
    rows = (
        (1, "Disabled target", "paz", 0, 10),
        (2, "Unrelated mod", "paz", 0, 20),
        (3, "Enabled target", "asi", 1, 30),
    )
    db.connection.executemany(
        "INSERT INTO mods "
        "(id, name, mod_type, enabled, priority) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    db.connection.executemany(
        "INSERT INTO mod_deltas "
        "(mod_id, file_path, delta_path, byte_start, byte_end) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            (1, "gamecommondata/target.paz", "deltas/1/target", 1, 2),
            (2, "gamecommondata/unrelated.paz", "deltas/2/keep", 3, 4),
            (3, "bin64/enabled.dll", "deltas/3/enabled", 5, 6),
        ),
    )
    db.connection.execute(
        "INSERT INTO mod_config (mod_id, selected_labels) VALUES (?, ?)",
        (2, '{"keep":true}'),
    )
    db.connection.commit()
    db.close()

    for mod_id, payload in (
        (1, b"target"),
        (2, b"unrelated"),
        (3, b"enabled"),
    ):
        delta_dir = cdmods_dir / "deltas" / str(mod_id)
        source_dir = cdmods_dir / "sources" / str(mod_id)
        delta_dir.mkdir(parents=True)
        source_dir.mkdir(parents=True)
        (delta_dir / "payload.bin").write_bytes(payload)
        (source_dir / "source.zip").write_bytes(payload + b"-source")

    return game_dir


def _remove_args(game_dir: Path, mod_id: int) -> SimpleNamespace:
    """Build the parsed argument surface consumed by ``cmd_remove_mod``."""
    return SimpleNamespace(game_dir=str(game_dir), mod_id=mod_id)


def _fetch_mod_rows(db_path: Path) -> list[tuple]:
    """Read the small set of fields used to prove unrelated-row stability."""
    db = Database(db_path)
    db.initialize()
    rows = db.connection.execute(
        "SELECT id, name, mod_type, enabled, priority "
        "FROM mods ORDER BY id"
    ).fetchall()
    db.close()
    return rows


def test_list_mods_help_requires_explicit_read_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Help advertises the explicit fail-closed inventory handshake."""
    from cdumm import cli

    monkeypatch.setattr(cli, "_attach_console", lambda: None)
    monkeypatch.setattr(sys, "argv", ["cdumm", "list-mods", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--read-only" in help_text
    assert "--json" in help_text
    assert "--game-dir" in help_text


def test_list_mods_without_read_only_is_rejected_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--read-only`` fails in argparse before touching SQLite."""
    from cdumm import cli

    game_dir = _seed_protocol_game(tmp_path, monkeypatch)
    before = _tree_snapshot(game_dir)
    monkeypatch.setattr(cli, "_attach_console", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cdumm", "list-mods", "--json", "--game-dir", str(game_dir)],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert _tree_snapshot(game_dir) == before


def test_list_mods_uses_mode_ro_query_only_without_initialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inventory reads neither initialize the schema nor mutate any file."""
    from cdumm import cli

    game_dir = _seed_protocol_game(tmp_path, monkeypatch)
    before = _tree_snapshot(game_dir)

    def _initialize_must_not_run(_self) -> None:
        raise AssertionError("list-mods must not initialize or migrate")

    monkeypatch.setattr(Database, "initialize", _initialize_must_not_run)
    args = SimpleNamespace(
        game_dir=str(game_dir),
        json=True,
        read_only=True,
        status=False,
        type=None,
    )
    cli.cmd_list_mods(args)

    inventory = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in inventory] == [1, 2, 3]
    assert _tree_snapshot(game_dir) == before

    db = cli._open_existing_db(game_dir, read_only=True)
    try:
        assert db.connection.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.connection.execute("DELETE FROM mods")
    finally:
        db.close()


def test_remove_help_and_main_entrypoint_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The packaged entrypoint routes remove-mod and its help is exact."""
    from cdumm import cli

    monkeypatch.setattr(cli, "_attach_console", lambda: None)
    monkeypatch.setattr(sys, "argv", ["cdumm", "remove-mod", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--mod-id" in help_text
    assert "--game-dir" in help_text

    main_source = (
        Path(__file__).parents[1] / "src" / "cdumm" / "main.py"
    ).read_text(encoding="utf-8")
    assert '"remove-mod"' in main_source


def test_version_identifies_exact_fork_protocol(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The packaged probe exposes both fork version and protocol identity."""
    from cdumm import cli

    monkeypatch.setattr(cli, "_attach_console", lambda: None)
    monkeypatch.setattr(sys, "argv", ["cdumm", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == (
        "CDUMM 3.6.0+sub432.1 (sublate.cdumm.v1)"
    )


def test_invalid_remove_id_is_nonzero_and_has_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Argparse rejects non-positive IDs before command dispatch."""
    from cdumm import cli

    game_dir = _seed_protocol_game(tmp_path, monkeypatch)
    before = _tree_snapshot(game_dir)
    monkeypatch.setattr(cli, "_attach_console", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cdumm",
            "remove-mod",
            "--mod-id",
            "0",
            "--game-dir",
            str(game_dir),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert _tree_snapshot(game_dir) == before


def test_enabled_remove_is_nonzero_path_free_and_has_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An enabled mod is rejected before any writable connection opens."""
    from cdumm import cli

    game_dir = _seed_protocol_game(tmp_path, monkeypatch)
    before = _tree_snapshot(game_dir)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_remove_mod(_remove_args(game_dir, 3))

    assert exc.value.code == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    output = json.loads(captured.err)
    assert output["error"]["code"] == "mod_enabled"
    assert str(tmp_path) not in json.dumps(output)
    assert _tree_snapshot(game_dir) == before


def test_disabled_remove_uses_domain_operation_and_preserves_unrelated_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful removal deletes only target-owned manager artifacts."""
    from cdumm import cli
    from cdumm.engine.mod_manager import ModManager

    game_dir = _seed_protocol_game(tmp_path, monkeypatch)
    cdmods_dir = game_dir / "CDMods"
    unrelated_before = _tree_snapshot(cdmods_dir / "deltas" / "2")
    original_remove = ModManager.remove_mod
    calls: list[tuple[int, bool, bool]] = []

    def _recording_remove(
        self,
        mod_id: int,
        *,
        require_existing: bool = False,
        require_disabled: bool = False,
    ) -> None:
        calls.append((mod_id, require_existing, require_disabled))
        original_remove(
            self,
            mod_id,
            require_existing=require_existing,
            require_disabled=require_disabled,
        )

    monkeypatch.setattr(ModManager, "remove_mod", _recording_remove)
    cli.cmd_remove_mod(_remove_args(game_dir, 1))

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "id": 1,
        "outcome": "removed",
    }
    assert calls == [(1, True, True)]
    assert _fetch_mod_rows(cdmods_dir / "cdumm.db") == [
        (2, "Unrelated mod", "paz", 0, 20),
        (3, "Enabled target", "asi", 1, 30),
    ]
    assert not (cdmods_dir / "deltas" / "1").exists()
    assert not (cdmods_dir / "sources" / "1").exists()
    assert _tree_snapshot(cdmods_dir / "deltas" / "2") == unrelated_before
    assert (cdmods_dir / "sources" / "2" / "source.zip").read_bytes() == (
        b"unrelated-source"
    )


def test_missing_remove_is_idempotent_success_with_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A retry after committed removal converges as ``already_absent``."""
    from cdumm import cli

    game_dir = _seed_protocol_game(tmp_path, monkeypatch)
    cli.cmd_remove_mod(_remove_args(game_dir, 1))
    capsys.readouterr()
    before_retry = _tree_snapshot(game_dir)

    cli.cmd_remove_mod(_remove_args(game_dir, 1))

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "id": 1,
        "outcome": "already_absent",
    }
    assert _tree_snapshot(game_dir) == before_retry


def test_filesystem_crash_leaves_retryable_row_then_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A crash after delta deletion rolls back DB state and can be retried."""
    from cdumm import cli
    from cdumm.engine import mod_manager

    game_dir = _seed_protocol_game(tmp_path, monkeypatch)
    cdmods_dir = game_dir / "CDMods"
    original_rmtree = shutil.rmtree
    interrupted = {"done": False}

    def _delete_then_interrupt(path: Path, *args, **kwargs) -> None:
        original_rmtree(path, *args, **kwargs)
        if not interrupted["done"]:
            interrupted["done"] = True
            raise OSError("synthetic interruption with a private path")

    monkeypatch.setattr(mod_manager.shutil, "rmtree", _delete_then_interrupt)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_remove_mod(_remove_args(game_dir, 1))
    assert exc.value.code == 5
    failure = json.loads(capsys.readouterr().err)
    assert failure["error"]["code"] == "remove_failed"
    assert str(tmp_path) not in json.dumps(failure)
    assert [row[0] for row in _fetch_mod_rows(
        cdmods_dir / "cdumm.db"
    )] == [1, 2, 3]
    assert not (cdmods_dir / "deltas" / "1").exists()
    assert (cdmods_dir / "sources" / "1").exists()

    monkeypatch.setattr(mod_manager.shutil, "rmtree", original_rmtree)
    cli.cmd_remove_mod(_remove_args(game_dir, 1))
    success = json.loads(capsys.readouterr().out)
    assert success == {"id": 1, "outcome": "removed"}
    assert [row[0] for row in _fetch_mod_rows(
        cdmods_dir / "cdumm.db"
    )] == [2, 3]
    assert not (cdmods_dir / "sources" / "1").exists()
