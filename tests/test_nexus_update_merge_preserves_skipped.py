"""GitHub #194 (GabrielNunesIT): "Check for Mod Updates not working —
there are new versions of installed mods but the check says
up-to-date."

Root cause from his bug report log: the startup check found 2 updates
(12:58:54 "set_nexus_updates: 2 updates"). A second check ~30 seconds
later feed-skipped those same mods — their updates were older than the
1-week recently-updated feed AND their nexus_last_checked_at had just
been stamped by the first check — so check_mod_updates returned results
for OTHER mods only. _apply_nexus_update_colors then REPLACED
self._nexus_updates wholesale with the new (smaller) dict, wiping the
known-outdated entries (12:59:28 "set_nexus_updates: 0 updates" /
"0 outdated"). The red pills vanished and every later check kept them
gone, because the feed-skip keeps trusting "checked recently".

Fix: merge each cycle's results over the previous state instead of
replacing. Feed-skipped mods keep their last known state (that is the
skip's own premise); freshly checked mods get their entries
overwritten; failure cycles (auth error, rate limit, transport) carry
an empty pending dict and must not wipe anything either.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Status:
    mod_id: int
    has_update: bool


def _merge(prev, new):
    from cdumm.gui.fluent_window import _merge_nexus_updates
    return _merge_nexus_updates(prev, new)


def test_feed_skipped_outdated_entries_survive_a_partial_cycle():
    """The #194 scenario: first cycle found mods 241 and 479 outdated;
    second cycle only fetched mods 56 and 350 (rest feed-skipped). The
    outdated entries must survive the second cycle."""
    prev = {
        241: _Status(241, True),
        479: _Status(479, True),
        56: _Status(56, False),
    }
    new = {
        56: _Status(56, False),
        350: _Status(350, False),
    }
    merged = _merge(prev, new)
    assert merged[241].has_update, (
        "mod 241's known update was wiped by a cycle that never "
        "re-checked it (the #194 disappearing-red-pill bug)")
    assert merged[479].has_update
    assert not merged[350].has_update
    assert set(merged) == {241, 479, 56, 350}


def test_fresh_result_overrides_stale_entry():
    """A mod re-checked this cycle gets its new state, both
    directions (outdated -> current after user updates, and
    current -> outdated when the author ships a new file)."""
    prev = {241: _Status(241, True), 479: _Status(479, False)}
    new = {241: _Status(241, False), 479: _Status(479, True)}
    merged = _merge(prev, new)
    assert not merged[241].has_update
    assert merged[479].has_update


def test_failure_cycle_keeps_previous_state():
    """Auth-error / rate-limit / transport-failure cycles set an empty
    pending dict; they must not blank the pills."""
    prev = {241: _Status(241, True)}
    merged = _merge(prev, {})
    assert merged == prev


def test_inputs_are_not_mutated():
    prev = {241: _Status(241, True)}
    new = {350: _Status(350, False)}
    merged = _merge(prev, new)
    merged[999] = _Status(999, True)
    assert 999 not in prev and 999 not in new
    assert set(prev) == {241} and set(new) == {350}
