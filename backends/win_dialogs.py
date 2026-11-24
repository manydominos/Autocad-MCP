"""Dialog windows of the CAD application, read and dismissed through Win32 - no COM.

Why this exists (agent mcp, v4): a modal dialog in AutoCAD (recover prompt,
security or font warning, crash report ...) blocks every ActiveX call. The COM
thread then waits inside ``Documents.Open`` and every later call is rejected
(``<unknown>.Count``), so nothing reachable over COM can say *what* AutoCAD is
waiting for. Win32 window enumeration does not go through AutoCAD's message
filter, so it still answers while AutoCAD is blocked.

Scope, deliberately small:

* :func:`list_dialogs` - visible top-level windows of the CAD process
  (``CAD_PROCESS_NAMES``, default ``acad.exe``) that are dialogs (class
  ``#32770``) or are owned by a disabled window (i.e. modal), with their
  title, the texts and buttons that Win32 can read and - when ``comtypes`` is
  installed - the texts UI Automation reads (a TaskDialog draws its text in a
  ``DirectUIHWND`` that Win32 cannot read).
* :func:`close_dialog` - only *Cancel*, *Esc* or *Close*. Never a named
  button: a "Yes"/"Recover" click may change the drawing, and choosing it is
  the operator's decision (decision of the user, 2026-11-07).

Runs in the server process, which must live in the same interactive session
as AutoCAD (it does: the MCP is started from the user's startup folder).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from typing import Any

log = logging.getLogger(__name__)

_WIN32 = sys.platform == "win32"

if _WIN32:
    try:
        import win32con
        import win32gui
        import win32process

        _WIN32_OK = True
    except ImportError:  # pragma: no cover - pywin32 is a hard requirement on Windows
        _WIN32_OK = False
else:  # pragma: no cover
    _WIN32_OK = False

#: The only actions :func:`close_dialog` performs.
CLOSE_ACTIONS = ("cancel", "esc", "close")

_DIALOG_CLASS = "#32770"
_TASKDIALOG_CHILD = "DirectUIHWND"
_IDCANCEL = 2
_BN_CLICKED = 0
_TDM_CLICK_BUTTON = 0x0400 + 102  # WM_USER + 102, task dialogs only
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_TEXTS = 40
_MAX_TEXT_CHARS = 2000


def process_names() -> set[str]:
    """Executable names that count as the CAD application (``CAD_PROCESS_NAMES``)."""
    raw = os.environ.get("CAD_PROCESS_NAMES", "").strip() or "acad.exe"
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def available() -> bool:
    return _WIN32_OK


def _exe_name(pid: int) -> str | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            ctypes.c_void_p(handle), 0, buf, ctypes.byref(size)
        ):
            return None
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _text(hwnd: int) -> str:
    try:
        return (win32gui.GetWindowText(hwnd) or "").strip()[:_MAX_TEXT_CHARS]
    except Exception:
        return ""


def _children(hwnd: int) -> list[int]:
    out: list[int] = []

    def _cb(child, _):
        out.append(child)
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:  # a window with no children raises on some pywin32 builds
        pass
    return out


def _uia_texts(hwnd: int) -> list[str] | None:
    """Names of every element under ``hwnd`` through UI Automation, or None.

    None when comtypes (or its pre-generated UIAutomationCore wrapper) is not
    available; the Win32 texts are then the only ones reported.
    """
    try:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
    except Exception as exc:
        log.debug("UIA unavailable: %s", exc)
        return None
    try:
        try:
            from comtypes.gen import UIAutomationClient as uia_mod
        except ImportError:
            uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation)
        root = uia.ElementFromHandle(hwnd)
        found = root.FindAll(uia_mod.TreeScope_Descendants, uia.CreateTrueCondition())
        texts: list[str] = []
        for i in range(min(found.Length, 400)):
            name = (found.GetElement(i).CurrentName or "").strip()
            if name and name not in texts:
                texts.append(name[:_MAX_TEXT_CHARS])
            if len(texts) >= _MAX_TEXTS:
                break
        return texts
    except Exception as exc:
        log.debug("UIA read of hwnd %s failed: %s", hwnd, exc)
        return None
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _describe(hwnd: int, pid: int, exe: str, *, use_uia: bool) -> dict[str, Any]:
    owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0
    texts: list[str] = []
    buttons: list[dict[str, Any]] = []
    task_dialog = False
    for child in _children(hwnd):
        try:
            cls = win32gui.GetClassName(child)
        except Exception:
            continue
        if cls == _TASKDIALOG_CHILD:
            task_dialog = True
            continue
        if not win32gui.IsWindowVisible(child):
            continue
        txt = _text(child)
        if cls == "Button":
            try:
                ctrl_id = win32gui.GetDlgCtrlID(child)
            except Exception:
                ctrl_id = None
            if txt:
                buttons.append({"id": ctrl_id, "text": txt.replace("&", "")})
        elif txt and txt not in texts and len(texts) < _MAX_TEXTS:
            texts.append(txt)
    row: dict[str, Any] = {
        "hwnd": int(hwnd),
        "title": _text(hwnd),
        "class": win32gui.GetClassName(hwnd),
        "pid": int(pid),
        "process": exe,
        "owner_hwnd": int(owner),
        "modal": bool(owner) and not win32gui.IsWindowEnabled(owner),
        "task_dialog": task_dialog,
        "texts": texts,
        "buttons": buttons,
        "text_source": "win32",
    }
    if use_uia and (task_dialog or not texts):
        uia = _uia_texts(hwnd)
        if uia is not None:
            row["texts"] = uia
            row["text_source"] = "uia"
    return row


def _cad_windows() -> list[tuple[int, int, str]]:
    """Visible top-level windows of the CAD processes: (hwnd, pid, exe)."""
    names = process_names()
    exe_by_pid: dict[int, str | None] = {}
    out: list[tuple[int, int, str]] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if pid not in exe_by_pid:
            exe_by_pid[pid] = _exe_name(pid)
        exe = exe_by_pid[pid]
        if exe and exe in names:
            out.append((hwnd, pid, exe))
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def list_dialogs(*, use_uia: bool = True) -> dict[str, Any]:
    """Dialogs and the blocked state of the CAD application's windows.

    ``blocked`` is True when a visible unowned (main) window of the CAD process
    is disabled - the Win32 sign of a modal dialog, even one this function
    cannot classify.
    """
    if not _WIN32_OK:
        return {"available": False, "reason": "win32_only", "dialogs": [], "blocked": False}
    dialogs: list[dict[str, Any]] = []
    main_windows: list[dict[str, Any]] = []
    for hwnd, pid, exe in _cad_windows():
        try:
            cls = win32gui.GetClassName(hwnd)
            owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0
            if not owner and cls != _DIALOG_CLASS:
                main_windows.append(
                    {
                        "hwnd": int(hwnd),
                        "title": _text(hwnd),
                        "enabled": bool(win32gui.IsWindowEnabled(hwnd)),
                    }
                )
                continue
            is_modal = bool(owner) and not win32gui.IsWindowEnabled(owner)
            if cls == _DIALOG_CLASS or is_modal:
                dialogs.append(_describe(hwnd, pid, exe, use_uia=use_uia))
        except Exception as exc:  # a window may vanish while it is read
            log.debug("window %s skipped: %s", hwnd, exc)
    return {
        "available": True,
        "process_names": sorted(process_names()),
        "dialogs": dialogs,
        "blocked": any(not w["enabled"] for w in main_windows),
        "main_windows": main_windows,
    }


def modal_dialogs(*, use_uia: bool = True) -> list[dict[str, Any]]:
    """Only the dialogs that block the application (modal ones)."""
    return [d for d in list_dialogs(use_uia=use_uia)["dialogs"] if d["modal"]]


def summarize(dialogs: list[dict[str, Any]]) -> str:
    """One line per dialog for an error message: title, texts and buttons."""
    parts = []
    for d in dialogs:
        texts = " | ".join(d.get("texts") or [])[:500]
        buttons = ", ".join(b["text"] for b in d.get("buttons") or [])
        parts.append(
            f"[hwnd {d['hwnd']}] '{d.get('title', '')}'"
            + (f": {texts}" if texts else "")
            + (f" (buttons: {buttons})" if buttons else "")
        )
    return "; ".join(parts)


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", ctypes.c_long * 4),
    ]


def _focus_of_thread(thread_id: int) -> int | None:
    """The window with the keyboard focus on ``thread_id`` (GetGUIThreadInfo)."""
    try:
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not ctypes.windll.user32.GetGUIThreadInfo(int(thread_id), ctypes.byref(info)):
            return None
        return int(info.hwndFocus) if info.hwndFocus else None
    except Exception as exc:  # pragma: no cover - Win32 only
        log.debug("GetGUIThreadInfo failed: %s", exc)
        return None


def main_windows() -> list[int]:
    """The CAD processes' visible main (unowned, non-dialog) windows."""
    if not _WIN32_OK:
        return []
    found = []
    for hwnd, _pid, _exe in _cad_windows():
        try:
            if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                continue
            if win32gui.GetClassName(hwnd) == _DIALOG_CLASS:
                continue
            found.append(int(hwnd))
        except Exception as exc:  # a window may vanish while it is read
            log.debug("window %s skipped: %s", hwnd, exc)
    return found


def send_escape(times: int = 3) -> dict[str, Any]:
    """Post ESC keystrokes to AutoCAD's command line - Win32 only, never COM.

    A command that stops at a prompt keeps ``SendCommand`` from returning,
    and no ActiveX call can answer the prompt (measured on 1.8-05), so the
    key is posted to the window that has the keyboard focus on the main
    window's GUI thread (the command line, usually), else to the main window.
    Only windows of the CAD processes are addressed (``process_names``).
    """
    if not _WIN32_OK:
        return {"sent": 0, "targets": [], "reason": "win32_only"}
    targets: list[int] = []
    for hwnd in main_windows():
        try:
            thread_id, _pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            thread_id = None
        focus = _focus_of_thread(thread_id) if thread_id else None
        target = focus or hwnd
        if target not in targets:
            targets.append(target)
    sent = 0
    for target in targets:
        for _ in range(max(1, int(times))):
            try:
                win32gui.PostMessage(target, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0x00010001)
                win32gui.PostMessage(target, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0xC0010001)
                sent += 1
            except Exception as exc:
                log.debug("ESC to %s failed: %s", target, exc)
    if sent:
        log.warning("ESC x%s posted to AutoCAD window(s) %s", times, targets)
    return {"sent": sent, "targets": targets}


def close_dialog(hwnd: int, action: str = "cancel", *, wait_s: float = 3.0) -> dict[str, Any]:
    """Dismiss one listed dialog with Cancel, Esc or Close - nothing else.

    Refused unless ``hwnd`` is a dialog :func:`list_dialogs` reports right now
    (so no window of another program can be targeted) and ``action`` is one
    of :data:`CLOSE_ACTIONS`.
    """
    if not _WIN32_OK:
        raise RuntimeError("system_dialog_close works only on Windows (win32_only)")
    action = (action or "cancel").strip().lower()
    if action not in CLOSE_ACTIONS:
        raise ValueError(
            f"action {action!r} is not allowed; only {', '.join(CLOSE_ACTIONS)} "
            "(a named button such as Yes/Recover is the operator's decision)"
        )
    current = {d["hwnd"]: d for d in list_dialogs(use_uia=False)["dialogs"]}
    if int(hwnd) not in current:
        raise ValueError(
            f"hwnd {hwnd} is not a dialog of {', '.join(sorted(process_names()))} right now; "
            f"call system_dialog_list (listed: {sorted(current) or 'none'})"
        )
    dialog = current[int(hwnd)]
    if action == "cancel":
        if dialog["task_dialog"]:
            win32gui.PostMessage(hwnd, _TDM_CLICK_BUTTON, _IDCANCEL, 0)
        else:
            win32gui.PostMessage(hwnd, win32con.WM_COMMAND, (_BN_CLICKED << 16) | _IDCANCEL, 0)
    elif action == "esc":
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0x00010001)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0xC0010001)
    else:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    log.warning(
        "system_dialog_close: %s sent to hwnd %s '%s'", action, hwnd, dialog.get("title", "")
    )
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        gone = not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd)
        if gone or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    after = list_dialogs(use_uia=False)
    return {
        "hwnd": int(hwnd),
        "title": dialog.get("title", ""),
        "action": action,
        "closed": bool(gone),
        "remaining_dialogs": [
            {"hwnd": d["hwnd"], "title": d["title"], "modal": d["modal"]} for d in after["dialogs"]
        ],
        "blocked": after.get("blocked", False),
    }