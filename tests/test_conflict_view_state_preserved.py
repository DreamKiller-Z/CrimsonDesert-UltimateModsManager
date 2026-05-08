"""Conflict tree must keep the user's expansion state across rebuilds.

Bug 2026-05-08: every call to ``ConflictView.update_conflicts`` did
``removeRows + rebuild + expandAll`` (when auto_expand=True). That wiped
the user's expansion choices, scroll position, and selection — every
mod toggle, apply, or background scan made the tree visibly snap back
to the top with all groups reset.

The fix captures expansion state by stable identity (mod-pair tuple
for top-level rows, file-path for child rows) and restores it after
the rebuild. ``auto_expand`` only fires on the FIRST population (no
prior data to preserve), not on subsequent refreshes.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


def _make_conflict(mod_a_id, mod_a_name, mod_b_id, mod_b_name,
                    file_path, level="byte_range",
                    explanation="overlap", winner_id=None,
                    winner_name=None):
    from cdumm.engine.conflict_detector import Conflict
    return Conflict(
        mod_a_id=mod_a_id, mod_a_name=mod_a_name,
        mod_b_id=mod_b_id, mod_b_name=mod_b_name,
        file_path=file_path, level=level,
        byte_start=0, byte_end=0,
        explanation=explanation,
        winner_id=winner_id, winner_name=winner_name)


def test_user_expansion_survives_rebuild(qtbot):
    """User expands one pair; refresh shouldn't collapse it."""
    from cdumm.gui.conflict_view import ConflictView, MOD_A_ID_ROLE, MOD_B_ID_ROLE

    view = ConflictView()
    qtbot.addWidget(view)

    conflicts = [
        _make_conflict(1, "Mod Alpha", 2, "Mod Beta", "ui/a.xml"),
        _make_conflict(3, "Mod Gamma", 4, "Mod Delta", "ui/b.xml"),
    ]
    # First population: pair rows are visible, default starts collapsed
    # because auto_expand=False simulates the conflicts dialog usage.
    view.update_conflicts(conflicts, auto_expand=False)

    # User collapses the first pair to verify default-collapsed, then
    # manually expands the SECOND pair only.
    pair_b_item = view._model.item(1, 0)
    assert pair_b_item is not None
    pair_b_idx = view._model.indexFromItem(pair_b_item)
    view._tree.expand(pair_b_idx)
    # Sanity: the user's manual expand is in effect.
    assert view._tree.isExpanded(pair_b_idx)

    # Refresh the tree with the same conflict set. Old code would call
    # expandAll() on auto_expand=True (and we'd lose the manual state),
    # OR keep all collapsed on auto_expand=False (also overwriting).
    view.update_conflicts(conflicts, auto_expand=False)

    # The same pair B item (different QStandardItem instance, same
    # identity) is still expanded. Pair A is still collapsed.
    pair_a_after = view._model.item(0, 0)
    pair_b_after = view._model.item(1, 0)
    assert pair_a_after is not None and pair_b_after is not None
    a_after_idx = view._model.indexFromItem(pair_a_after)
    b_after_idx = view._model.indexFromItem(pair_b_after)
    assert view._tree.isExpanded(b_after_idx), (
        "User-expanded pair B was collapsed across rebuild")
    assert not view._tree.isExpanded(a_after_idx), (
        "Default-collapsed pair A unexpectedly opened on rebuild")


def test_first_population_still_auto_expands(qtbot):
    """auto_expand=True on FIRST population should expand all rows
    so users get a quick overview of small conflict sets, even though
    subsequent refreshes preserve user state instead."""
    from cdumm.gui.conflict_view import ConflictView

    view = ConflictView()
    qtbot.addWidget(view)

    conflicts = [
        _make_conflict(1, "Mod A", 2, "Mod B", "ui/x.xml"),
    ]
    view.update_conflicts(conflicts, auto_expand=True)

    pair_item = view._model.item(0, 0)
    assert pair_item is not None
    assert view._tree.isExpanded(view._model.indexFromItem(pair_item)), (
        "First-population auto_expand=True should expand the pair row")


def test_empty_conflicts_does_not_freeze_tree(qtbot):
    """The empty-conflicts branch used to early-return without
    re-enabling updates / unblocking signals, leaving the tree frozen."""
    from cdumm.gui.conflict_view import ConflictView

    view = ConflictView()
    qtbot.addWidget(view)

    # Populate first so we can confirm the tree was previously updating.
    view.update_conflicts(
        [_make_conflict(1, "Mod A", 2, "Mod B", "ui/x.xml")],
        auto_expand=False,
    )
    assert view._tree.updatesEnabled()

    # Then call with empty list; the early-return path runs.
    view.update_conflicts([], auto_expand=True)

    # Tree must still be ACCEPTING updates after the empty path.
    assert view._tree.updatesEnabled(), (
        "Empty conflicts left the tree with updates disabled forever")
    assert not view._model.signalsBlocked(), (
        "Empty conflicts left the model with signals blocked forever")
