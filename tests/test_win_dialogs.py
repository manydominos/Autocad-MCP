"""v4 (agent mcp): AutoCAD dialogs read and dismissed through Win32.

A real modal dialog is raised by a helper process (``python.exe`` showing a
MessageBox owned by its own window) and ``CAD_PROCESS_NAMES`` points the module
at that process, so the whole path runs without AutoCAD.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid

import pytest

pytest.importorskip("win32gui", reason="pywin32 not installed")

from backends import win_dialogs  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 only")

_HELPER = r"""
import ctypes, sys, win32con, win32gui
owner = win32gui.CreateWindow("Static", "ZN glavno okno", win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
                              10, 10, 300, 120, 0, 0, 0, None)
r = ctypes.windll.user32.MessageBoxW(owner, "Besedilo testnega okna", sys.argv[1], 0x1)
sys.exit(r)
"""

# A venv's python.exe is a launcher that runs the base interpreter as a child,
# so the dialog belongs to another pid than Popen's: rows are matched by a
# title unique to the test instead.


def _rows(title, *, use_uia=False):
    return [d for d in win_dialogs.list_dialogs(use_uia=use_uia)["dialogs"] if d["title"] == title]


def _find(proc, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(proc.title)
        if rows:
            proc.dialog_pid = rows[0]["pid"]
            return rows[0]
        time.sleep(0.2)
    raise AssertionError("helper dialog did not appear")


@pytest.fixture
def dialog_proc(monkeypatch):
    monkeypatch.setenv("CAD_PROCESS_NAMES", "python.exe")
    title = f"ZN pogovorno okno {uuid.uuid4().hex[:8]}"
    proc = subprocess.Popen([sys.executable, "-I", "-c", _HELPER, title])
    proc.title, proc.dialog_pid = title, None
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        if proc.dialog_pid:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.dialog_pid)], capture_output=True)


def test_process_names_default_and_override(monkeypatch):
    monkeypatch.delenv("CAD_PROCESS_NAMES", raising=False)
    assert win_dialogs.process_names() == {"acad.exe"}
    monkeypatch.setenv("CAD_PROCESS_NAMES", "ACAD.exe, python.exe")
    assert win_dialogs.process_names() == {"acad.exe", "python.exe"}


def test_modal_dialog_is_listed_with_title_text_and_buttons(dialog_proc):
    row = _find(dialog_proc)
    assert row["title"] == dialog_proc.title
    assert row["class"] == "#32770"
    assert row["modal"] is True
    assert "Besedilo testnega okna" in row["texts"]
    assert {b["id"] for b in row["buttons"]} >= {1, 2}
    listed = win_dialogs.list_dialogs(use_uia=False)
    assert listed["blocked"] is True
    assert row["hwnd"] in {d["hwnd"] for d in win_dialogs.modal_dialogs(use_uia=False)}
    assert dialog_proc.title in win_dialogs.summarize([row])


def test_uia_text_reading_does_not_break_the_listing(dialog_proc):
    _find(dialog_proc)
    rows = _rows(dialog_proc.title, use_uia=True)
    assert rows and "Besedilo testnega okna" in rows[0]["texts"]
    assert rows[0]["text_source"] in ("win32", "uia")


@pytest.mark.parametrize("action", ["cancel", "esc", "close"])
def test_close_dismisses_the_dialog_with_cancel(dialog_proc, action):
    row = _find(dialog_proc)
    result = win_dialogs.close_dialog(row["hwnd"], action)
    assert result["closed"] is True and result["action"] == action
    assert dialog_proc.wait(10) == 2  # IDCANCEL - never IDOK


@pytest.mark.parametrize("action", ["yes", "ok", "recover", "IDOK"])
def test_any_other_action_is_refused_before_anything_is_sent(dialog_proc, action):
    row = _find(dialog_proc)
    with pytest.raises(ValueError, match="not allowed"):
        win_dialogs.close_dialog(row["hwnd"], action)
    assert dialog_proc.poll() is None


def test_a_window_that_is_not_a_listed_dialog_is_refused(dialog_proc):
    _find(dialog_proc)
    import win32gui

    desktop = win32gui.GetDesktopWindow()
    with pytest.raises(ValueError, match="is not a dialog"):
        win_dialogs.close_dialog(desktop, "cancel")
    assert dialog_proc.poll() is None


def test_no_cad_process_means_no_dialogs(monkeypatch):
    monkeypatch.setenv("CAD_PROCESS_NAMES", "ni_takega_programa.exe")
    listed = win_dialogs.list_dialogs()
    assert listed["available"] is True and listed["dialogs"] == [] and listed["blocked"] is False