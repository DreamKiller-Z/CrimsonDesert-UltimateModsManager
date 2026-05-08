"""Two mods both ship a full-replace bsdiff for the same .paz file.

CDUMM used to apply BOTH bsdiff deltas sequentially. Each bsdiff is
computed against the vanilla base, so feeding the second one a non-
vanilla input (the first mod's output) produces a corrupted paz: the
bsdiff control stream issues copies/inserts that don't reconcile with
the actual base bytes, the result is truncated, PAMT entries point
past the end of the file, and the game crashes on launch.

Bug confirmed 2026-05-08 against Graphics Mod (Nexus 651) +
Vaxis Water Physics Overhaul Mod (Nexus 2376) both full-replacing
0003/0.paz: staged paz was 756,563 bytes but PAMT expected 816,735.

The fix: when multiple full-replace deltas target the same file,
apply only the priority winner; warn about the skipped ones.
"""
from __future__ import annotations

from pathlib import Path

import bsdiff4
import pytest


def _setup_engine(tmp_path: Path, vanilla_bytes: bytes):
    """Build a minimal ApplyWorker that can run _compose_file for one
    file with full-replace bsdiff deltas. Returns (engine, captured_warnings_list)."""
    from cdumm.engine.apply_engine import ApplyWorker

    engine = ApplyWorker.__new__(ApplyWorker)
    engine._soft_warnings = []

    captured: list[str] = []

    class _Sig:
        def emit(self, msg):
            captured.append(msg)
    engine.warning = _Sig()

    # _compose_file reads vanilla from _vanilla_dir using
    # ``file_path.replace("/", "\\")``. We just write the vanilla bytes
    # at that exact relative path inside tmp_path.
    engine._vanilla_dir = tmp_path / "vanilla"
    engine._game_dir = tmp_path / "game"
    engine._vanilla_dir.mkdir()
    engine._game_dir.mkdir()
    (engine._vanilla_dir / "0003").mkdir()
    (engine._vanilla_dir / "0003" / "0.paz").write_bytes(vanilla_bytes)

    # Stub helpers _compose_file calls. None of these matter for the
    # full-replace branch we want to test.
    engine._merge_json_patch_deltas = lambda fp, ds: ([], ds)
    engine._try_semantic_merge = lambda fp, eds: eds
    engine._overlay_entries = []
    return engine, captured


def test_only_priority_winner_full_replace_applies(tmp_path):
    vanilla = b"VANILLA_PAZ_BYTES_" * 100  # 1800 bytes
    mod_a_output = b"MOD_A_OUTPUT_BYTES_" * 100
    mod_b_output = b"MOD_B_OUTPUT_BYTES_" * 100

    # Build bsdiffs against vanilla (both BSDI4-magic patches)
    delta_a = tmp_path / "mod_a.bsdiff"
    delta_b = tmp_path / "mod_b.bsdiff"
    delta_a.write_bytes(bsdiff4.diff(vanilla, mod_a_output))
    delta_b.write_bytes(bsdiff4.diff(vanilla, mod_b_output))
    assert delta_a.read_bytes()[:4] == b"BSDI"
    assert delta_b.read_bytes()[:4] == b"BSDI"

    engine, captured = _setup_engine(tmp_path, vanilla)

    deltas = [
        # Lower priority value = higher precedence in CDUMM.
        # Mod A has priority 27 (lower precedence).
        # Mod B has priority 18 (higher precedence — this one wins).
        {
            "delta_path": str(delta_a),
            "mod_name": "Graphics Mod",
            "priority": 27,
            "kind": "byte",
        },
        {
            "delta_path": str(delta_b),
            "mod_name": "Vaxis Water Physics",
            "priority": 18,
            "kind": "byte",
        },
    ]

    result = engine._compose_file("0003/0.paz", deltas)
    assert result == mod_b_output, (
        f"Expected the higher-priority winner's bytes, got "
        f"{result[:20]!r}... — the bug applied both bsdiffs "
        f"sequentially and produced corrupt bytes (expected "
        f"{mod_b_output[:20]!r}...)."
    )
    assert any("Graphics Mod" in m for m in captured), (
        f"Expected a warning naming the dropped mod, captured: "
        f"{captured!r}")
    assert any("conflicting full-replace" in m.lower() for m in captured)


def test_single_full_replace_still_applies_normally(tmp_path):
    vanilla = b"VANILLA_BYTES_" * 50
    modded = b"MODDED_BYTES_" * 50
    delta = tmp_path / "mod.bsdiff"
    delta.write_bytes(bsdiff4.diff(vanilla, modded))

    engine, captured = _setup_engine(tmp_path, vanilla)
    deltas = [{
        "delta_path": str(delta),
        "mod_name": "Solo Mod",
        "priority": 5,
        "kind": "byte",
    }]
    result = engine._compose_file("0003/0.paz", deltas)
    assert result == modded
    assert not any("conflicting" in m.lower() for m in captured)
