"""Grid-shaping guardrails for the Game Data table preview.

Regression for the doubled ``_key`` column: several game-data tables list
``_key`` (and sometimes ``_name``) among their own schema fields, and
``_shape_records`` already prepends ``_key`` / ``_name`` as the leading
columns — so those must be dropped from the schema-field list or the grid
shows a redundant duplicate column (seen on ``sequencerspawninfo``).
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qfluentwidgets")


def _schema(*names, verified=None):
    return SimpleNamespace(
        fields=[SimpleNamespace(name=n) for n in names],
        verified_fields=verified)


def test_shape_records_dedupes_key_and_name_columns():
    from cdumm.gui.pages.game_data_page import _shape_records
    # mirrors sequencerspawninfo: schema lists _key among its fields
    schema = _schema("_isRandom", "_stageType", "_key", "_isBlocked")
    records = {
        1001: {"_key": 1001, "_name": "A", "_isRandom": 26,
               "_stageType": 0, "_isBlocked": 236},
        1002: {"_key": 1002, "_name": "B", "_isRandom": 23,
               "_stageType": 0, "_isBlocked": 236},
    }
    cols, rows, total, _health = _shape_records(records, schema)
    assert cols.count("_key") == 1          # leading col only, not duplicated
    assert cols.count("_name") == 1
    assert cols == ["_key", "_name", "_isRandom", "_stageType", "_isBlocked"]
    assert total == 2
    assert rows[0][0] == "1001" and rows[0][1] == "A"


def test_shape_records_without_schema():
    from cdumm.gui.pages.game_data_page import _shape_records
    cols, _rows, _total, health = _shape_records(
        {1: {"_key": 1, "_name": "x"}}, None)
    assert cols == ["_key", "_name"] and health == 0.0


def test_shape_records_position_column():
    from cdumm.gui.pages.game_data_page import _shape_records
    schema = _schema("_isRandom")
    records = {
        1: {"_key": 1, "_name": "A", "_isRandom": 5},
        2: {"_key": 2, "_name": "B", "_isRandom": 6},
    }
    positions = {1: (-11534.5, 530.4, -6126.3)}      # only key 1 has a position
    cols, rows, total, _h = _shape_records(records, schema, positions)
    assert cols == ["_key", "_name", "world pos (X, Y, Z)", "_isRandom"]
    assert rows[0][2] == "-11534.5, 530.4, -6126.3"
    assert rows[1][2] == ""                           # key 2 → blank, not guessed
    # no positions → no extra column
    cols2, _r, _t, _h2 = _shape_records(records, schema)
    assert "world pos (X, Y, Z)" not in cols2


def test_shape_records_verified_only_masks_unverified_fields():
    """A hand-curated table shows only validated fields; the rest render
    ``(unverified)`` instead of a possibly-wrong value, and unverified
    columns don't drag the health score."""
    from cdumm.gui.pages.game_data_page import _shape_records
    schema = _schema("_increasePrice", "_isBlocked", "_useTargetPrice",
                     verified=frozenset({"_increasePrice"}))
    records = {
        256: {"_key": 256, "_name": "", "_increasePrice": 100,
              "_isBlocked": 0, "_useTargetPrice": 0},
        262: {"_key": 262, "_name": "", "_increasePrice": 1500,
              "_isBlocked": 0, "_useTargetPrice": 1},
    }
    cols, rows, _total, health = _shape_records(records, schema)
    ci = {c: i for i, c in enumerate(cols)}
    # verified field shows its real value
    assert rows[0][ci["_increasePrice"]] == "100"
    assert rows[1][ci["_increasePrice"]] == "1500"
    # unverified fields are masked, never shown as a decoded guess
    assert rows[0][ci["_isBlocked"]] == "(unverified)"
    assert rows[0][ci["_useTargetPrice"]] == "(unverified)"
    # only the (varying) verified field is scored → healthy
    assert health == 0.0


def test_shape_records_verified_none_shows_all_fields():
    """Backward compat: verified_fields=None → every field decoded as before."""
    from cdumm.gui.pages.game_data_page import _shape_records
    schema = _schema("_a", "_b", verified=None)
    records = {1: {"_key": 1, "_name": "x", "_a": 7, "_b": 9}}
    _cols, rows, _total, _h = _shape_records(records, schema)
    assert rows[0][-2:] == ["7", "9"]         # no masking anywhere
