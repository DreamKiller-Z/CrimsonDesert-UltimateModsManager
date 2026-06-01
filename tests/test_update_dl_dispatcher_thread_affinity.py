"""Regression for GitHub #170 (jikulopo / Elec0 / devCKVargas / AwfulLon).

The v3.3.15 attempt at #170 added Qt.QueuedConnection to the worker
signal connect calls in fluent_window._start_direct_update_download.
That looked correct on paper but jikulopo's instrumented v3.3.15 run
proved the slot still ran on the worker thread: PySide6 6.x routes a
QueuedConnection to a free Python callable onto the SENDER's thread,
not the receiver's, because there is no QObject receiver to anchor the
queue.

The real fix is to route the worker signals through a small QObject
parented to the main window. The bound methods of that QObject have
main-thread affinity, so Qt's QueuedConnection queues the call to the
main-thread event loop where InfoBar.success and QTimer.singleShot
behave correctly.

These tests pin down both halves:
  * the bug pattern reproduces (free callable runs on worker thread)
  * the fix pattern works (dispatcher bound method runs on main thread)

If someone "simplifies" the dispatcher back to a free callable, the
second test fails. If PySide6 ever changes QueuedConnection semantics
so free callables ARE main-thread-routed, the first test fails — and
this whole indirection can be removed.
"""
import threading

import pytest
from PySide6.QtCore import QObject, QThread, Qt, Signal

from cdumm.gui.fluent_window import _UpdateDLDispatcher


class _DoneEmitter(QObject):
    done = Signal(str)

    def fire(self) -> None:
        self.done.emit("/fake/path")


def _drive_thread_emit(qtbot, emitter, connect_target, connection_type):
    """Move ``emitter`` to a worker thread, fire its signal, return the
    thread id the connected slot ran on."""
    captured = {}

    def slot_proxy(*args):
        # Used only by the free-callable branch; the dispatcher branch
        # routes through its own bound method which already captures.
        captured["tid"] = threading.get_ident()

    thread = QThread()
    emitter.moveToThread(thread)
    if connect_target is None:
        emitter.done.connect(slot_proxy, connection_type)
    else:
        emitter.done.connect(connect_target, connection_type)
    thread.started.connect(emitter.fire)
    emitter.done.connect(thread.quit)

    with qtbot.waitSignal(thread.finished, timeout=5000):
        thread.start()

    return captured


def test_free_callable_queued_runs_on_worker_thread(qtbot):
    """The bug pattern v3.3.15 shipped: this should run on the worker
    thread, proving why the v3.3.15 fix didn't actually work."""
    main_tid = threading.get_ident()
    emitter = _DoneEmitter()
    captured = _drive_thread_emit(
        qtbot, emitter, None, Qt.ConnectionType.QueuedConnection)
    assert captured["tid"] != main_tid, (
        "If this assertion ever flips, PySide6 changed QueuedConnection "
        "semantics for free callables and the _UpdateDLDispatcher "
        "indirection can be removed.")


def test_dispatcher_bound_method_runs_on_main_thread(qtbot):
    """The fix pattern: dispatcher parented to a main-thread QObject."""
    main_tid = threading.get_ident()
    parent = QObject()  # stands in for the main window

    captured = {}

    def on_done(path: str) -> None:
        captured["tid"] = threading.get_ident()
        captured["path"] = path

    dispatch = _UpdateDLDispatcher(
        parent, on_progress=lambda *a: None, on_done=on_done,
        on_failed=lambda *a: None)

    emitter = _DoneEmitter()
    _drive_thread_emit(
        qtbot, emitter, dispatch.done, Qt.ConnectionType.QueuedConnection)

    assert captured.get("tid") == main_tid
    assert captured.get("path") == "/fake/path"


def test_dispatcher_swallows_callback_exceptions(qtbot):
    """A raise inside the user callback must not crash the dispatcher
    (the update download path is best-effort UI, not critical state)."""
    parent = QObject()

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    dispatch = _UpdateDLDispatcher(
        parent, on_progress=boom, on_done=boom, on_failed=boom)

    # All three slots must return cleanly even though the callback raises.
    dispatch.progress(1, 2)
    dispatch.done("/x")
    dispatch.failed("nope")
