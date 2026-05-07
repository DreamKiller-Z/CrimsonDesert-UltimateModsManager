"""Tests for the ConfigPanel resize handle (Task 2.1)."""
from __future__ import annotations

import pytest


@pytest.fixture
def app(qtbot):
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _show_simple_mod(panel, qtbot):
    panel.show_mod(
        mod_id=1, name="t", author="x", version="1",
        status="active", file_count=1,
        patches=[{"label": "p", "enabled": True}],
        conflicts=[],
    )


def test_panel_has_resize_handle(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    assert hasattr(panel, "_resize_handle")
    assert panel._resize_handle is not None


def test_resize_handle_cursor_is_size_hor(qtbot, app):
    from PySide6.QtCore import Qt
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    assert panel._resize_handle.cursor().shape() == Qt.CursorShape.SizeHorCursor


def test_set_panel_width_clamps_to_min(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_panel_width(100)  # below min
    assert panel._PANEL_WIDTH == 480


def test_set_panel_width_clamps_to_max(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_panel_width(2000)  # above max
    assert panel._PANEL_WIDTH == 1200


def test_set_panel_width_in_range(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    panel.set_panel_width(800)
    assert panel._PANEL_WIDTH == 800


def test_set_panel_width_resizes_visible_panel(qtbot, app):
    from cdumm.gui.components.config_panel import ConfigPanel
    panel = ConfigPanel()
    qtbot.addWidget(panel)
    _show_simple_mod(panel, qtbot)
    panel.set_panel_width(800)
    # The panel's actual width should reflect the new value
    # (animation may still be running, but maxWidth should be set).
    assert panel.maximumWidth() == 800
