"""Preset detection must recognize "same-offset mutex + shared label
prefix + always-on siblings" packs, not only ``[Tag]``-prefixed packs.

Bug 2026-05-09 (Zowbaid, Nexus): Unlimited Dragon Flying mod 356
v3.1 ships 7 patches in one JSON:

  Dragon Call Cooldown (60 min -> 1 sec)             [always-on]
  Ride Duration: 30 Minutes                          [variant]
  Ride Duration: 60 Minutes                          [variant]
  Ride Duration: 120 Minutes (2 Hours)               [variant]
  Ride Duration: 240 Minutes (4 Hours)               [variant]
  Ride Duration: Max (Effectively Unlimited)         [variant]
  Dragon HP Regen (200 -> 1,000,000,000)             [always-on]

The five Ride Duration variants all target the same byte offset
(23357109), so they're mutually exclusive. The other two patches
have unique offsets and are independent toggles.

Pre-fix ``detect_preset_groups`` only recognized ``[Tag]`` prefixed
labels and required EVERY label to belong to some group. Because
none of these patches use bracket prefixes, the detector returned
None. The user got 7 flat checkboxes including 5 mutually exclusive
ones, and had to manually pick which Ride Duration to enable.

Fix: when ``[Tag]``-prefix detection fails, fall back to grouping
by ``(game_file, offset)``. Multiple patches at the same offset are
mutually exclusive. If 2+ at one offset share a meaningful label
prefix, that's a preset family. Patches outside any same-offset
mutex group are returned as ``__always_on__`` indices so the UI
can keep them as independent toggles instead of resetting them
when a preset radio is clicked.
"""
from __future__ import annotations


def _make_dragon_patches() -> list[dict]:
    return [
        {"label": "Dragon Call Cooldown (60 min -> 1 sec)",
         "game_file": "gamedata/characterinfo.pabgb",
         "offset": 23357101},
        {"label": "Ride Duration: 30 Minutes",
         "game_file": "gamedata/characterinfo.pabgb",
         "offset": 23357109},
        {"label": "Ride Duration: 60 Minutes",
         "game_file": "gamedata/characterinfo.pabgb",
         "offset": 23357109},
        {"label": "Ride Duration: 120 Minutes (2 Hours)",
         "game_file": "gamedata/characterinfo.pabgb",
         "offset": 23357109},
        {"label": "Ride Duration: 240 Minutes (4 Hours)",
         "game_file": "gamedata/characterinfo.pabgb",
         "offset": 23357109},
        {"label": "Ride Duration: Max (Effectively Unlimited)",
         "game_file": "gamedata/characterinfo.pabgb",
         "offset": 23357109},
        {"label": "Dragon HP Regen (200 -> 1,000,000,000)",
         "game_file": "gamedata/characterinfo.pabgb",
         "offset": 23359357},
    ]


def test_detects_five_ride_duration_variants_as_one_preset_family():
    from cdumm.gui.components.config_panel import detect_preset_groups
    groups = detect_preset_groups(_make_dragon_patches())

    assert groups is not None, (
        "detector must surface a preset family for mod 356; got None"
    )
    # Strip the magic always-on key for the variant count.
    variant_keys = [k for k in groups if not k.startswith("__")]
    assert len(variant_keys) == 5, (
        f"expected 5 ride-duration variants; got {variant_keys}"
    )


def test_two_always_on_patches_surface_as_independent_indices():
    from cdumm.gui.components.config_panel import detect_preset_groups
    groups = detect_preset_groups(_make_dragon_patches())

    assert groups is not None
    always_on = groups.get("__always_on__", [])
    # Cooldown (idx 0) and HP Regen (idx 6) are independent toggles.
    assert sorted(always_on) == [0, 6], (
        f"always-on patches at unique offsets must be surfaced as "
        f"__always_on__ indices; got {always_on}"
    )


def test_each_variant_indexes_a_single_patch():
    from cdumm.gui.components.config_panel import detect_preset_groups
    groups = detect_preset_groups(_make_dragon_patches())

    assert groups is not None
    variant_keys = [k for k in groups if not k.startswith("__")]
    for k in variant_keys:
        assert len(groups[k]) == 1, (
            f"each Ride Duration variant should map to exactly one "
            f"patch index; group {k!r} has {groups[k]}"
        )
    # Every variant index must be within the same-offset mutex (idx 1-5)
    all_variant_indices = [i for k in variant_keys for i in groups[k]]
    assert sorted(all_variant_indices) == [1, 2, 3, 4, 5]


def test_backward_compat_tag_prefix_still_works():
    """Sanity: the existing ``[Tag]`` prefix path must keep working
    so mod 1103 (12 percent presets, every label has [N%] prefix)
    doesn't regress."""
    from cdumm.gui.components.config_panel import detect_preset_groups
    patches = [
        {"label": "[0%] foo"},
        {"label": "[25%] foo"},
        {"label": "[50%] foo"},
        {"label": "[100%] foo"},
    ]
    groups = detect_preset_groups(patches)
    assert groups is not None
    variant_keys = sorted(k for k in groups if not k.startswith("__"))
    assert variant_keys == ["0%", "100%", "25%", "50%"], (
        f"[Tag] prefix detection regressed: {variant_keys}"
    )


def test_no_mutex_family_returns_none():
    """If no offset has 2+ patches AND no [Tag] prefix exists,
    the detector should return None (flat checkbox UI)."""
    from cdumm.gui.components.config_panel import detect_preset_groups
    patches = [
        {"label": "Faster intro", "game_file": "x", "offset": 100},
        {"label": "Skip splash", "game_file": "x", "offset": 200},
        {"label": "Disable cutscene", "game_file": "x", "offset": 300},
    ]
    assert detect_preset_groups(patches) is None


def test_offset_mutex_without_meaningful_prefix_returns_none():
    """Two patches at the same offset but with totally unrelated
    labels are not a preset family (unrelated mutex variants).
    The detector should not invent a meaningless grouping."""
    from cdumm.gui.components.config_panel import detect_preset_groups
    patches = [
        {"label": "Apple Bonus",
         "game_file": "x", "offset": 100},
        {"label": "Banana Bonus",
         "game_file": "x", "offset": 100},
    ]
    # No common 3+ char prefix between "Apple Bonus" and "Banana Bonus";
    # detector should not synthesize a fake family.
    result = detect_preset_groups(patches)
    # Either None, or a result that does NOT group these two into
    # the same family.
    if result is not None:
        variant_keys = [k for k in result if not k.startswith("__")]
        # If detector returned anything, every variant must be alone
        # — no fake "Bonus" family pulling them together.
        for k in variant_keys:
            assert len(result[k]) <= 1, (
                f"detector synthesized a meaningless family for "
                f"unrelated mutex labels; group {k!r}={result[k]}"
            )
