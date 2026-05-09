"""Preset radio row must not clip its radios at narrow panel widths.

Bug 2026-05-08 (wootwoots, Nexus comment on the CDUMM mod page):
"There is an issue with the configuration tab, can't horizontally
resize the tab and / or there is no horizontal scroll when the
radio option list overflow, therefore I can't see all the available
radio options. At least have a vertical display for the radio
would do the job, JMM was doing something like that and it worked
very well."

Repro: a Format 3 mod that ships 8+ tagged preset variants. Mod
1103 (12 percent presets) is the canonical case. CDUMM packs every
variant tag into a single ``QHBoxLayout`` with ``addStretch()``
appended, no wrap and no scroll. At the 480px minimum panel width,
12 short percent radios + Custom (13 total) physically need
~700+ pixels and Qt clips the rightmost handful so the user can
never click them.

Fix: the preset row must wrap to a second line when it can't fit
on one. A ``qfluentwidgets.FlowLayout`` is the cleanest substitute
for ``QHBoxLayout`` here — it behaves identically when there's room
and wraps when there isn't.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _show_panel_with_n_percent_presets(qtbot, panel, n: int) -> None:
    """Show a mod with N percent-tagged patches so the preset
    detector fires and renders N+1 radios (N tags + Custom).

    Skips past the panel's open-animation by finishing it
    immediately, then waits an event-loop tick for FlowLayout
    geometry to settle so callers can measure radio positions
    deterministically (the animation is 250ms; without forcing it
    the test would be racing easing-curve interpolation)."""
    tags = [f"{int(round(100 * i / max(n - 1, 1)))}%" for i in range(n)]
    seen: set[str] = set()
    unique: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    while len(unique) < n:
        unique.append(f"{len(unique) * 7}%")
    patches = [
        {"label": f"[{t}] foo", "enabled": True} for t in unique[:n]
    ]
    panel.show_mod(
        mod_id=1, name="overflow-test", author="x", version="1",
        status="active", file_count=1, patches=patches, conflicts=[],
    )
    # Force the maximumWidth animation to land at its end value so
    # panel.width() == panel._PANEL_WIDTH for measurements.
    if panel._anim.state() != panel._anim.State.Stopped:
        panel._anim.setCurrentTime(panel._anim.duration())
        panel._anim.stop()
    panel.setMaximumWidth(panel._PANEL_WIDTH)
    panel.resize(panel._PANEL_WIDTH, panel.height() or 600)
    qtbot.wait(50)


def test_preset_row_wraps_when_too_many_tags_for_panel_width(
        qtbot, app):
    """At the minimum 480px panel width, 13 radios (12 percent
    tags + Custom) cannot physically fit on a single row. They
    MUST be laid out across multiple rows so the user can reach
    every option."""
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)

    panel.set_panel_width(panel._MIN_PANEL_WIDTH)
    qtbot.wait(50)

    _show_panel_with_n_percent_presets(qtbot, panel, n=12)
    qtbot.wait(50)

    assert panel._preset_radio_group is not None, (
        "preset detector should have fired for 12 percent-tagged "
        "patches, but no radio group was created"
    )

    # 12 tags + 1 Custom = 13 radios.
    radios = panel._preset_radio_group.buttons()
    assert len(radios) == 13

    # Bucket each radio's top-left Y position. With a flat
    # QHBoxLayout all radios share one row -> 1 distinct Y bucket.
    # With wrap (FlowLayout / vertical fallback) there are 2+
    # buckets. Bucket size 4px tolerates sub-pixel layout drift.
    y_buckets: set[int] = set()
    for rb in radios:
        y_buckets.add(rb.geometry().top() // 4)

    assert len(y_buckets) >= 2, (
        f"preset row at panel width {panel.width()} did not wrap: "
        f"all 13 radios share one row (Y buckets: {sorted(y_buckets)}). "
        f"At this width, the rightmost radios are off-screen and "
        f"the user cannot click them."
    )


def test_every_preset_radio_is_within_panel_horizontal_bounds(
        qtbot, app):
    """Stronger contract: every individual radio's right edge must
    sit within the panel's horizontal bounds. This catches both
    'one-row-overflow' (right edge past panel.width()) and
    'wrap-but-still-clipped' regressions."""
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)

    panel.set_panel_width(panel._MIN_PANEL_WIDTH)
    qtbot.wait(50)

    _show_panel_with_n_percent_presets(qtbot, panel, n=12)
    qtbot.wait(50)

    assert panel._preset_radio_group is not None
    panel_width = panel.width()

    for rb in panel._preset_radio_group.buttons():
        # Map the radio's rect into the panel's coordinate system.
        top_left = rb.mapTo(panel, rb.rect().topLeft())
        bottom_right = rb.mapTo(panel, rb.rect().bottomRight())
        assert bottom_right.x() <= panel_width, (
            f"radio {rb.text()!r} extends past panel right edge: "
            f"right={bottom_right.x()}, panel_width={panel_width}. "
            f"User cannot click this radio."
        )
        # Also: not negative-x (off the left edge).
        assert top_left.x() >= 0, (
            f"radio {rb.text()!r} starts at x={top_left.x()} (off "
            f"the left edge of the panel)"
        )


def test_short_preset_list_still_uses_single_row(qtbot, app):
    """Sanity: when the row fits on one line (small N, short tags),
    the radios must still all share a row. This verifies the fix
    doesn't pessimistically wrap a tiny list across multiple rows."""
    from cdumm.gui.components.config_panel import ConfigPanel

    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)

    panel.set_panel_width(panel._DEFAULT_PANEL_WIDTH)  # 640
    qtbot.wait(50)

    # 2 percent tags + Custom = 3 radios. Cannot possibly need >1 row.
    _show_panel_with_n_percent_presets(qtbot, panel, n=2)
    qtbot.wait(50)

    radios = panel._preset_radio_group.buttons()
    assert len(radios) == 3

    y_buckets = {rb.geometry().top() // 4 for rb in radios}
    assert len(y_buckets) == 1, (
        f"3 radios at 640px width should fit on one row, but they "
        f"wrapped to {len(y_buckets)} rows. Fix is over-eager."
    )
