"""system_run_command: unknown command refused before sending; ESC for a stuck prompt.

Fork branch popravek/ukaz-esc. On AutoCAD 2021 (2026-11-23) ``-POINTCLOUDATTACH``
stopped at its insertion-point prompt and blocked COM until a human pressed
ESC (three times), and ``-POINTCLOUDCROP`` - which does not exist - made
AutoCAD run the rest of the string as a LINE. No ActiveX call answers a
prompt, so the ESC goes through Win32 (``win_dialogs.send_escape``).
"""

from __future__ import annotations

import asyncio
import threading
import types

import pytest
from fastmcp.exceptions import ToolError

import config
from backends import win_dialogs
from engineering.errors import CommandIncompleteError
from security_lisp import READ, assess

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "command,token",
    [
        ("_-POINTCLOUDCROP 0,0\n", "-POINTCLOUDCROP"),
        ("._line 0,0 1,1\n\n", "LINE"),
        ("_.-LAYER _S 0\n\n", "-LAYER"),
        ("_ZOOM;_E", "ZOOM"),
        ("REGENALL", "REGENALL"),
        ('(command "_LINE")', None),
        ("'_ZOOM _E", None),
        ("", None),
        ("   ", None),
        ('_PNGOUT "C:/x.png"', "PNGOUT"),
        ("3DORBIT", None),
        ("$x", None),
    ],
)
async def test_first_command_token(command, token):
    from backends.com_backend import first_command_token

    assert first_command_token(command) == token


class _Doc:
    def __init__(self, known: dict[str, bool]):
        self.vars = {"CMDACTIVE": 0, "USERS1": "operator"}
        self.known = known
        self.sent = []

    def GetVariable(self, name):
        return self.vars[name]

    def SetVariable(self, name, value):
        self.vars[name] = value

    def SendCommand(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith('(setvar "USERS1"'):
            name = cmd.split('(getcname "', 1)[1].split('"', 1)[0]
            self.vars["USERS1"] = "1" if self.known.get(name, False) else "0"


def _outcome(prompt):
    return types.SimpleNamespace(last_prompt=prompt, report=lambda: {"completed": True})


@pytest.fixture
def com(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    doc = _Doc({})

    def _fake_send(d, cmd, *a, **k):
        d.sent.append(cmd)
        return _outcome("Command:")

    monkeypatch.setattr(module, "_acad_doc", lambda: doc)
    monkeypatch.setattr(module.ComBackend, "_safe_send_command", staticmethod(_fake_send))
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, doc


async def test_unknown_command_is_refused_and_nothing_sent(com):
    backend, doc = com
    with pytest.raises(ToolError, match="unknown_command.*-POINTCLOUDCROP"):
        await backend.system_run_command("_-POINTCLOUDCROP 0,0 10,10\n")
    assert len(doc.sent) == 1 and doc.sent[0].startswith("(setvar")  # only the check
    assert doc.vars["USERS1"] == "operator"  # restored


async def test_known_command_runs_and_reports_the_check(com):
    backend, doc = com
    doc.known["LINE"] = True
    result = await backend.system_run_command("_LINE 0,0 1,1\n\n")
    assert result["ok"] is True and result["command_checked"] == "LINE"
    assert doc.sent[-1] == "_LINE 0,0 1,1\n\n"


async def test_check_command_false_skips_the_check(com):
    backend, doc = com
    result = await backend.system_run_command("L 0,0 1,1\n\n", check_command=False)
    assert result["ok"] is True and "command_checked" not in result
    assert doc.sent == ["L 0,0 1,1\n\n"]


async def test_a_failed_check_never_refuses(com, monkeypatch):
    backend, doc = com

    def _boom(*a, **k):
        raise RuntimeError("COM hiccup")

    monkeypatch.setattr(doc, "SendCommand", _boom)
    assert backend._command_known_sync("LINE") is None
    assert doc.vars["USERS1"] == "operator"


async def test_busy_autocad_is_not_checked(com):
    backend, doc = com
    doc.vars["CMDACTIVE"] = 1
    assert backend._command_known_sync("LINE") is None
    assert doc.sent == []


async def test_stuck_prompt_is_answered_with_escape(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    doc = _Doc({"-POINTCLOUDATTACH": True})
    released = threading.Event()

    def _blocking_send(d, cmd, *a, **k):
        assert released.wait(5), "ESC never arrived"
        return _outcome("*Cancel*")

    escapes = []

    def _send_escape(times=3):
        escapes.append(times)
        released.set()
        return {"sent": times, "targets": [123]}

    monkeypatch.setattr(module, "_acad_doc", lambda: doc)
    monkeypatch.setattr(module.ComBackend, "_safe_send_command", staticmethod(_blocking_send))
    monkeypatch.setattr(win_dialogs, "available", lambda: True)
    monkeypatch.setattr(win_dialogs, "send_escape", _send_escape)
    monkeypatch.setattr(config.settings, "command_escape_after_s", 0.2)
    backend = module.ComBackend()

    async def _run_thread(func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_thread)
    with pytest.raises(CommandIncompleteError) as info:
        await backend.system_run_command('_-POINTCLOUDATTACH "x.rcs"\n')
    assert escapes == [3]
    assert info.value.last_prompt == "*Cancel*"
    assert info.value.kind == "command_incomplete"


async def test_fast_command_sends_no_escape(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    backend = module.ComBackend()
    called = []
    monkeypatch.setattr(win_dialogs, "available", lambda: True)
    monkeypatch.setattr(win_dialogs, "send_escape", lambda times=3: called.append(1))
    monkeypatch.setattr(config.settings, "command_escape_after_s", 0.5)

    async def _run_fast(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_fast)
    escaped: dict = {}
    assert await backend._run_escaping(lambda: "done", escaped) == "done"
    assert called == [] and escaped == {}


async def test_default_escape_is_two_seconds_inside_the_com_deadline(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    backend = module.ComBackend()
    seen = {}
    real_wait = asyncio.wait

    async def _spy_wait(tasks, timeout=None):
        seen["timeout"] = timeout
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(config.settings, "command_escape_after_s", None)
    monkeypatch.setattr(config.settings, "com_call_timeout", 15.0)
    monkeypatch.setattr(module.asyncio, "wait", _spy_wait)

    async def _run_fast(func, *args, **kwargs):
        return func()

    monkeypatch.setattr(backend, "_run", _run_fast)
    assert await backend._run_escaping(lambda: 1, {}) == 1
    assert seen["timeout"] == 13.0


async def test_send_escape_without_cad_windows(monkeypatch):
    monkeypatch.setattr(win_dialogs, "_cad_windows", lambda: [])
    report = win_dialogs.send_escape()
    assert report["sent"] == 0 and report["targets"] == []


async def test_getcname_is_a_read_form():
    verdict = assess('(getcname "_-POINTCLOUDATTACH")')
    assert verdict.tier == READ, verdict.findings
