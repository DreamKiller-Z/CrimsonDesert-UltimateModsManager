"""Headless CLI for CDUMM — used by external tools (crash monitor, scripts).

Commands:
    CDUMM.exe list-mods --read-only [--json]
    CDUMM.exe remove-mod --mod-id ID --game-dir PATH
    CDUMM.exe set-enabled --mod-id ID --enabled true|false
    CDUMM.exe apply [--game-dir PATH]
    CDUMM.exe cleanup-duplicates [--dry-run] [--game-dir PATH]
"""
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from cdumm.engine.cdmods_paths import get_cdmods_root
from cdumm.platform import IS_WINDOWS, app_data_dir

APP_DATA_DIR = app_data_dir()


class _ExistingDatabase:
    """Minimal database adapter for a schema that already exists on disk.

    Protocol commands deliberately do not call :class:`Database.initialize`:
    initialization creates directories, runs migrations, creates indexes, and
    trims the activity log.  Those are appropriate application-startup side
    effects, but they violate the external protocol's promise that inventory
    reads and rejected removals do not mutate manager state.

    The adapter exposes the ``connection`` and ``close`` surface consumed by
    ``Config`` and ``ModManager`` while making the chosen SQLite access mode
    explicit at construction time.
    """

    def __init__(self, db_path: Path, *, read_only: bool) -> None:
        """Open ``db_path`` without creating or migrating it.

        Read-only connections use SQLite's URI ``mode=ro`` and also set
        ``query_only`` as defense in depth.  Writable connections are only
        created after a separate read-only preflight has proved that a remove
        target exists and is already disabled.
        """
        self.db_path = db_path
        if read_only:
            uri = f"{db_path.resolve().as_uri()}?mode=ro"
            self._connection = sqlite3.connect(uri, uri=True)
            self._connection.execute("PRAGMA query_only=ON")
            query_only = self._connection.execute(
                "PRAGMA query_only"
            ).fetchone()
            if not query_only or query_only[0] != 1:
                self._connection.close()
                raise sqlite3.OperationalError(
                    "SQLite refused query_only mode"
                )
        else:
            uri = f"{db_path.resolve().as_uri()}?mode=rw"
            self._connection = sqlite3.connect(uri, uri=True)
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the already-open connection expected by domain services."""
        return self._connection

    def close(self) -> None:
        """Close the protocol connection without running shutdown writes."""
        self._connection.close()


def _protocol_json(payload: dict) -> str:
    """Serialize a protocol response with deterministic key ordering."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _exit_protocol_error(code: str, message: str, exit_code: int) -> None:
    """Emit a path-free machine-readable error and terminate non-zero.

    Raw SQLite and filesystem exceptions can contain absolute tester paths.
    Protocol callers therefore receive a bounded error code and message while
    detailed diagnostics remain available to an interactive developer through
    a debugger or targeted test.
    """
    print(
        _protocol_json({
            "error": {"code": code, "message": message},
            "ok": False,
        }),
        file=sys.stderr,
    )
    raise SystemExit(exit_code)


def _positive_mod_id(value: str) -> int:
    """Parse a strictly positive mod ID before any command side effect."""
    try:
        mod_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "mod ID must be a positive integer"
        ) from exc
    if mod_id <= 0:
        raise argparse.ArgumentTypeError(
            "mod ID must be a positive integer"
        )
    return mod_id


def _cdmods_root(game_dir: Path, db=None) -> Path:
    """Return the CDMods root, honoring config override when a db is open."""
    if db is None:
        return get_cdmods_root(None, game_dir)
    from cdumm.storage.config import Config
    return get_cdmods_root(Config(db), game_dir)


def _attach_console():
    """Attach to parent console for windowed exe (console=False in PyInstaller).

    Windows-only: the macOS / Linux builds run from a real terminal so
    stdout/stderr are already wired correctly.
    """
    if IS_WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = open("CONOUT$", "w")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _resolve_game_dir(override: str | None = None) -> Path | None:
    """Find game directory from override, pointer file, or DB."""
    if override:
        p = Path(override)
        if p.exists():
            return p

    pointer = APP_DATA_DIR / "game_dir.txt"
    if pointer.exists():
        saved = pointer.read_text(encoding="utf-8").strip()
        if saved and Path(saved).exists():
            return Path(saved)

    return None


def _open_db(game_dir: Path):
    from cdumm.storage.database import Database
    db_path = get_cdmods_root(None, game_dir) / "cdumm.db"
    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    db = Database(db_path)
    db.initialize()
    return db


def _existing_db_path(game_dir: Path) -> Path:
    """Resolve the existing manager database without creating directories."""
    return get_cdmods_root(None, game_dir) / "cdumm.db"


def _open_existing_db(
    game_dir: Path,
    *,
    read_only: bool,
) -> _ExistingDatabase:
    """Open the existing manager database in the requested protocol mode."""
    db_path = _existing_db_path(game_dir)
    if not db_path.is_file():
        raise FileNotFoundError("CDUMM database is unavailable")
    return _ExistingDatabase(db_path, read_only=read_only)


def cmd_list_mods(args):
    """List manager inventory through a mandatory read-only SQLite session."""
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        _exit_protocol_error(
            "game_dir_unavailable",
            "Cannot find game directory; provide --game-dir.",
            1,
        )

    try:
        db = _open_existing_db(game_dir, read_only=True)
    except (FileNotFoundError, OSError, sqlite3.Error):
        _exit_protocol_error(
            "database_unavailable",
            "CDUMM database is unavailable.",
            1,
        )

    try:
        from cdumm.engine.mod_manager import ModManager
        mgr = ModManager(db, _cdmods_root(game_dir, db) / "deltas")
        mods = mgr.list_mods(args.type)

        if args.json:
            out = []
            for m in mods:
                entry = {
                    "id": m["id"],
                    "name": m["name"],
                    "mod_type": m["mod_type"],
                    "enabled": m["enabled"],
                    "priority": m["priority"],
                }
                if args.status:
                    entry["status"] = mgr.get_mod_game_status(
                        m["id"], game_dir
                    )
                out.append(entry)
            print(json.dumps(out, indent=2))
        else:
            for m in mods:
                if args.status:
                    game_status = mgr.get_mod_game_status(
                        m["id"], game_dir
                    )
                    print(
                        f"[{game_status:>12s}] #{m['id']:>3d}  "
                        f"{m['name']}  ({m['mod_type']})"
                    )
                else:
                    status = "ON " if m["enabled"] else "OFF"
                    print(
                        f"[{status}] #{m['id']:>3d}  "
                        f"{m['name']}  ({m['mod_type']})"
                    )
    except sqlite3.Error:
        _exit_protocol_error(
            "inventory_read_failed",
            "CDUMM inventory could not be read.",
            5,
        )
    finally:
        db.close()


def cmd_remove_mod(args):
    """Remove one already-disabled mod using the existing domain operation.

    The read-only preflight rejects absent and enabled rows before any writable
    SQLite connection is opened.  The second validation happens under
    ``BEGIN IMMEDIATE`` so another process cannot enable the mod between the
    check and ``ModManager.remove_mod``.  Filesystem failures leave the row in
    place, making a retry converge without touching unrelated mod state.
    """
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        _exit_protocol_error(
            "game_dir_unavailable",
            "Cannot find game directory; provide --game-dir.",
            1,
        )

    try:
        inspection_db = _open_existing_db(game_dir, read_only=True)
        try:
            row = inspection_db.connection.execute(
                "SELECT name, enabled FROM mods WHERE id = ?",
                (args.mod_id,),
            ).fetchone()
        finally:
            inspection_db.close()
    except (FileNotFoundError, OSError, sqlite3.Error):
        _exit_protocol_error(
            "database_unavailable",
            "CDUMM database is unavailable.",
            1,
        )

    if row is None:
        print(_protocol_json({
            "action": "already_absent",
            "mod_id": args.mod_id,
            "ok": True,
        }))
        return
    if bool(row[1]):
        _exit_protocol_error(
            "mod_enabled",
            "Disable and apply or revert the mod before removal.",
            4,
        )

    from cdumm.engine.mod_manager import (
        ModManager,
        ModMustBeDisabledError,
        ModNotFoundError,
    )

    db = None
    try:
        db = _open_existing_db(game_dir, read_only=False)
        db.connection.execute("BEGIN IMMEDIATE")
        mgr = ModManager(db, _cdmods_root(game_dir, db) / "deltas")
        mgr.remove_mod(
            args.mod_id,
            require_existing=True,
            require_disabled=True,
        )
    except ModNotFoundError:
        if db is not None:
            db.connection.rollback()
        print(_protocol_json({
            "action": "already_absent",
            "mod_id": args.mod_id,
            "ok": True,
        }))
        return
    except ModMustBeDisabledError:
        if db is not None:
            db.connection.rollback()
        _exit_protocol_error(
            "mod_enabled",
            "Disable and apply or revert the mod before removal.",
            4,
        )
    except (OSError, sqlite3.Error):
        if db is not None:
            db.connection.rollback()
        _exit_protocol_error(
            "remove_failed",
            "The mod could not be removed; manager state is retryable.",
            5,
        )
    finally:
        if db is not None:
            db.close()

    print(_protocol_json({
        "action": "removed",
        "mod_id": args.mod_id,
        "name": row[0],
        "ok": True,
    }))


def cmd_set_enabled(args):
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory. Use --game-dir.", file=sys.stderr)
        sys.exit(1)

    enabled = args.enabled.lower() in ("true", "1", "yes", "on")

    db = _open_db(game_dir)
    from cdumm.engine.mod_manager import ModManager
    mgr = ModManager(db, _cdmods_root(game_dir, db) / "deltas")

    # Verify mod exists
    mods = mgr.list_mods()
    mod = next((m for m in mods if m["id"] == args.mod_id), None)
    if not mod:
        print(f"Error: mod ID {args.mod_id} not found.", file=sys.stderr)
        db.close()
        sys.exit(1)

    mgr.set_enabled(args.mod_id, enabled)
    state = "enabled" if enabled else "disabled"
    print(f"{mod['name']} (#{args.mod_id}) {state}")
    db.close()


def cmd_cleanup_duplicates(args):
    """Find and merge duplicate mod rows.

    Symptom: drag-and-drop batch import in older builds skipped the
    name+version dedup gate, so re-importing a folder of all your
    mods doubled every row in the DB. This command finds groups of
    rows that share a name, picks the one most likely to be 'the
    real install' (applied, then enabled, then most-Nexus-metadata,
    then highest priority, then newest), copies any data only the
    siblings had into the kept row, and removes the rest along with
    their delta + source folders.

    --dry-run prints the plan and exits.
    """
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory. Use --game-dir.",
              file=sys.stderr)
        sys.exit(1)

    db_path = get_cdmods_root(None, game_dir) / "cdumm.db"
    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    from cdumm.engine.mod_dedup import (
        find_duplicate_groups, plan_cleanup, apply_cleanup,
    )
    from cdumm.engine.mod_manager import ModManager
    from cdumm.storage.database import Database

    db = Database(db_path)
    db.initialize()
    groups = find_duplicate_groups(db.connection)
    if not groups:
        print("No duplicate mod rows found. Nothing to do.")
        db.close()
        return

    plan = plan_cleanup(db.connection)
    total_dupes = sum(len(d) for _, d, _ in plan)
    print(f"Found {len(plan)} duplicate group(s) covering "
          f"{total_dupes} stale row(s):")
    print()
    for canon, deleted, update in plan:
        applied_flag = " [applied]" if canon.applied else ""
        print(f"  {canon.name}")
        print(f"    KEEP   id={canon.id} ver={canon.version!r} "
              f"fid={canon.nexus_real_file_id} "
              f"prio={canon.priority}{applied_flag}")
        for d in deleted:
            print(f"    DELETE id={d.id} ver={d.version!r} "
                  f"fid={d.nexus_real_file_id} prio={d.priority}")
        if update:
            print(f"    MERGE  copy {update} onto kept row")
        print()

    if args.dry_run:
        print("--dry-run set — no changes written.")
        db.close()
        return

    deltas_dir = _cdmods_root(game_dir, db) / "deltas"
    mgr = ModManager(db, deltas_dir)
    results = apply_cleanup(mgr)
    print(f"Cleanup complete. Removed "
          f"{sum(len(d) for _, d in results)} duplicate row(s).")
    db.close()


def cmd_apply(args):
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory. Use --game-dir.", file=sys.stderr)
        sys.exit(1)

    db_path = get_cdmods_root(None, game_dir) / "cdumm.db"
    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    # Open DB just to read the cdmods_path override for vanilla_dir.
    from cdumm.storage.database import Database as _Db
    _bootstrap_db = _Db(db_path)
    _bootstrap_db.initialize()
    vanilla_dir = _cdmods_root(game_dir, _bootstrap_db) / "vanilla"
    _bootstrap_db.close()

    # ApplyWorker needs PySide6 for QObject/Signal — import it
    from cdumm.engine.apply_engine import ApplyWorker

    worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)

    errors = []

    def on_progress(pct, msg):
        print(f"[{pct:3d}%] {msg}", file=sys.stderr)

    def on_error(msg):
        errors.append(msg)
        print(f"ERROR: {msg}", file=sys.stderr)

    def on_warning(msg):
        # Loop R1: ApplyWorker.warning carries soft warnings (size-merge
        # fallback drops, vanilla extraction failures from #62, etc.).
        # GUI surfaces them via InfoBar; CLI must print to stderr or
        # users debugging via headless apply will not see why their mod
        # silently produced no changes.
        print(f"WARNING: {msg}", file=sys.stderr)

    worker.progress_updated.connect(on_progress)
    worker.error_occurred.connect(on_error)
    worker.warning.connect(on_warning)

    worker.run()

    if errors:
        sys.exit(1)
    else:
        print("Apply complete.", file=sys.stderr)
        sys.exit(0)


def cmd_launch_game(args):
    """GitHub #63: apply enabled mods, then launch the game.

    Fail-fast: if apply emits any error, exit non-zero WITHOUT
    launching the game. Users on handheld devices (Steam Deck, ROG
    Ally) register CDUMM as a non-Steam launcher and press Play
    once; launching into a broken state would be worse than
    surfacing the apply error.
    """
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory. Use --game-dir.",
              file=sys.stderr)
        sys.exit(1)

    db_path = get_cdmods_root(None, game_dir) / "cdumm.db"
    if not db_path.exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    # Open DB just to read cdmods_path override.
    from cdumm.storage.database import Database as _Db
    _bootstrap_db = _Db(db_path)
    _bootstrap_db.initialize()
    vanilla_dir = _cdmods_root(game_dir, _bootstrap_db) / "vanilla"
    _bootstrap_db.close()

    from cdumm.engine.apply_engine import ApplyWorker

    worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)

    errors = []

    def on_progress(pct, msg):
        print(f"[{pct:3d}%] {msg}", file=sys.stderr)

    def on_error(msg):
        errors.append(msg)
        print(f"ERROR: {msg}", file=sys.stderr)

    def on_warning(msg):
        # Loop R1: surface soft warnings to stderr (same reason as
        # cmd_apply — handheld users running --launch-game from a
        # script need to see real warnings, not just success/error).
        print(f"WARNING: {msg}", file=sys.stderr)

    worker.progress_updated.connect(on_progress)
    worker.error_occurred.connect(on_error)
    worker.warning.connect(on_warning)

    worker.run()

    if errors:
        print("Apply failed; not launching game.", file=sys.stderr)
        sys.exit(1)

    print("Apply complete; launching game...", file=sys.stderr)
    try:
        from cdumm.engine import launcher
        launcher.launch_game(game_dir)
    except FileNotFoundError as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Launch failed: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def cmd_bisect(args):
    """Interactive bisection via CLI. Two sub-modes:
        bisect start [--mod-ids 1,2,3]  → starts session, applies first config, prints JSON state
        bisect report --crashed true/false → reports result, applies next config, prints JSON state
    """
    game_dir = _resolve_game_dir(args.game_dir)
    if not game_dir:
        print("Error: cannot find game directory.", file=sys.stderr)
        sys.exit(1)

    db = _open_db(game_dir)
    from cdumm.engine.mod_manager import ModManager
    from cdumm.engine.binary_search import DeltaDebugSession
    from cdumm.engine.apply_engine import ApplyWorker

    mgr = ModManager(db, _cdmods_root(game_dir, db) / "deltas")

    if args.action == "start":
        # Optionally filter to specific mod IDs
        if args.mod_ids:
            filter_ids = set(int(x) for x in args.mod_ids.split(","))
            # Disable mods NOT in the filter list
            for m in mgr.list_mods():
                if m["enabled"] and m["id"] not in filter_ids:
                    mgr.set_enabled(m["id"], False)

        session = DeltaDebugSession(mgr)
        config = session.start_round()

        # Apply the config
        for mod_id, enabled in config.items():
            mgr.set_enabled(mod_id, enabled)

        vanilla_dir = _cdmods_root(game_dir, db) / "vanilla"
        db_path = get_cdmods_root(None, game_dir) / "cdumm.db"
        worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)
        worker.progress_updated.connect(lambda pct, msg: print(f"[{pct:3d}%] {msg}", file=sys.stderr))
        worker.run()

        # Save session state
        _save_bisect_session(db, session)

        # Output state as JSON
        testing = [session.get_mod_name(m) for m in session.current_group]
        print(json.dumps({
            "phase": session.phase,
            "round": session.round_number,
            "testing": testing,
            "testing_count": len(testing),
            "total_suspects": len(session.all_ids),
            "status": session.get_phase_description(),
        }))

    elif args.action == "report":
        # Load saved session
        session = _load_bisect_session(db, mgr)
        if not session:
            print("Error: no active bisection session. Run 'bisect start' first.", file=sys.stderr)
            sys.exit(1)

        crashed = args.crashed.lower() in ("true", "1", "yes")
        status_msg = session.report_crash(crashed)
        print(f"Result: {status_msg}", file=sys.stderr)

        if session.is_done():
            culprit_ids = set(session._changes)
            culprits = [session.get_mod_name(m) for m in culprit_ids]
            # Restore original state BUT keep culprits disabled
            for mod_id, enabled in session.original_state.items():
                if mod_id in culprit_ids:
                    mgr.set_enabled(mod_id, False)
                else:
                    mgr.set_enabled(mod_id, enabled)
            vanilla_dir = _cdmods_root(game_dir, db) / "vanilla"
            db_path = get_cdmods_root(None, game_dir) / "cdumm.db"
            worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)
            worker.progress_updated.connect(lambda pct, msg: print(f"[{pct:3d}%] {msg}", file=sys.stderr))
            worker.run()
            _clear_bisect_session(db)
            print(json.dumps({
                "phase": "done",
                "culprits": culprits,
                "rounds": session.round_number,
            }))
        else:
            # Apply next config
            config = session.start_round()
            for mod_id, enabled in config.items():
                mgr.set_enabled(mod_id, enabled)
            vanilla_dir = _cdmods_root(game_dir, db) / "vanilla"
            db_path = get_cdmods_root(None, game_dir) / "cdumm.db"
            worker = ApplyWorker(game_dir, vanilla_dir, db_path, force_outdated=False)
            worker.progress_updated.connect(lambda pct, msg: print(f"[{pct:3d}%] {msg}", file=sys.stderr))
            worker.run()
            _save_bisect_session(db, session)
            testing = [session.get_mod_name(m) for m in session.current_group]
            print(json.dumps({
                "phase": session.phase,
                "round": session.round_number,
                "testing": testing,
                "testing_count": len(testing),
                "status": status_msg,
            }))

    db.close()


def _save_bisect_session(db, session):
    data = json.dumps({
        "changes": session._changes,
        "n": session._n,
        "partition_index": session._partition_index,
        "testing_complement": session._testing_complement,
        "test_set": session._test_set,
        "current_group": session.current_group,
        "round_number": session.round_number,
        "history": session.history,
        "phase": session.phase,
        "all_ids": session.all_ids,
        "original_state": {int(k): v for k, v in session.original_state.items()},
    })
    db.connection.execute(
        "CREATE TABLE IF NOT EXISTS ddmin_progress (id INTEGER PRIMARY KEY, data TEXT)")
    db.connection.execute(
        "INSERT OR REPLACE INTO ddmin_progress (id, data) VALUES (1, ?)", (data,))
    db.connection.commit()


def _load_bisect_session(db, mgr):
    from cdumm.engine.binary_search import DeltaDebugSession
    try:
        row = db.connection.execute(
            "SELECT data FROM ddmin_progress WHERE id = 1").fetchone()
        if not row:
            return None
        saved = json.loads(row[0])
        session = DeltaDebugSession(mgr)
        session._changes = saved["changes"]
        session._n = saved["n"]
        session._partition_index = saved["partition_index"]
        session._testing_complement = saved["testing_complement"]
        session.round_number = saved["round_number"]
        session.history = saved["history"]
        session.phase = saved["phase"]
        session.all_ids = saved["all_ids"]
        # JSON dict keys are always strings — convert back to int
        session.original_state = {int(k): v for k, v in saved["original_state"].items()}
        # Restore _test_set and current_group (critical for report_crash)
        if "test_set" in saved:
            session._test_set = saved["test_set"]
            session.current_group = saved.get("current_group", list(session._test_set))
        else:
            # Recompute from algorithm state (backward compat)
            partitions = session._split(session._changes, session._n)
            if session._partition_index < len(partitions):
                if not session._testing_complement:
                    session._test_set = partitions[session._partition_index]
                else:
                    session._test_set = [
                        mid for mid in session._changes
                        if mid not in partitions[session._partition_index]
                    ]
            else:
                session._test_set = list(session._changes)
            session.current_group = list(session._test_set)
        return session
    except Exception:
        return None


def _clear_bisect_session(db):
    try:
        db.connection.execute("DELETE FROM ddmin_progress WHERE id = 1")
        db.connection.commit()
    except Exception:
        pass


def main():
    _attach_console()
    _setup_logging()

    parser = argparse.ArgumentParser(
        prog="cdumm",
        description="CDUMM command-line interface for external tool integration.",
    )
    sub = parser.add_subparsers(dest="command")

    # list-mods
    p_list = sub.add_parser("list-mods", help="List mods")
    p_list.add_argument(
        "--read-only",
        action="store_true",
        required=True,
        help=(
            "Required protocol guard: open SQLite with mode=ro and "
            "query_only=ON"
        ),
    )
    p_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_list.add_argument("--status", action="store_true", help="Include game file status (active/not applied)")
    p_list.add_argument("--type", default=None, help="Filter by mod_type (paz, asi)")
    p_list.add_argument("--game-dir", default=None, help="Game directory override")

    # remove-mod
    p_remove = sub.add_parser(
        "remove-mod",
        help="Remove one already-disabled mod from manager-owned state",
    )
    p_remove.add_argument(
        "--mod-id",
        type=_positive_mod_id,
        required=True,
        help="Positive mod ID",
    )
    p_remove.add_argument(
        "--game-dir",
        required=True,
        help="Game directory override",
    )

    # set-enabled
    p_set = sub.add_parser("set-enabled", help="Enable or disable a mod")
    p_set.add_argument("--mod-id", type=int, required=True, help="Mod ID")
    p_set.add_argument("--enabled", required=True, help="true or false")
    p_set.add_argument("--game-dir", default=None, help="Game directory override")

    # apply
    p_apply = sub.add_parser("apply", help="Apply current mod state to game files")
    p_apply.add_argument("--game-dir", default=None, help="Game directory override")

    # launch-game (GitHub #63: handheld one-shot)
    p_launch = sub.add_parser(
        "launch-game",
        help="Apply enabled mods then launch Crimson Desert (fail-fast on apply error)")
    p_launch.add_argument("--game-dir", default=None, help="Game directory override")

    # cleanup-duplicates
    p_dedup = sub.add_parser(
        "cleanup-duplicates",
        help="Merge & remove duplicate mod rows (e.g. after a batch re-import)")
    p_dedup.add_argument("--dry-run", action="store_true",
                          help="Print the plan without modifying the DB")
    p_dedup.add_argument("--game-dir", default=None,
                          help="Game directory override")

    # bisect
    p_bisect = sub.add_parser("bisect", help="Binary search for problem mod")
    p_bisect.add_argument("action", choices=["start", "report"], help="start or report")
    p_bisect.add_argument("--mod-ids", default=None, help="Comma-separated mod IDs to test (optional)")
    p_bisect.add_argument("--crashed", default=None, help="true/false — did the game crash?")
    p_bisect.add_argument("--game-dir", default=None, help="Game directory override")

    args = parser.parse_args()

    if args.command == "list-mods":
        cmd_list_mods(args)
    elif args.command == "remove-mod":
        cmd_remove_mod(args)
    elif args.command == "set-enabled":
        cmd_set_enabled(args)
    elif args.command == "apply":
        cmd_apply(args)
    elif args.command == "launch-game":
        cmd_launch_game(args)
    elif args.command == "bisect":
        cmd_bisect(args)
    elif args.command == "cleanup-duplicates":
        cmd_cleanup_duplicates(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
