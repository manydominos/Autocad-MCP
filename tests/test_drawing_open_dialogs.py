"""v4 (agent mcp): ``drawing_open`` on the live engine against a fake ActiveX surface.

P2026-015: ``Documents.Open`` of a DXF never returned within COM_CALL_TIMEOUT
and AutoCAD rejected calls afterwards (a modal dialog). Pinned here: the dialog
pre-check, the separate OPEN_CALL_TIMEOUT, the dialog named in the timeout
error and the wait-out of a rejected read after ``Open``.
"""

from __future__ import annotations

import time
import types
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("win32com.client", reason="pywin32 not installed")

import config  # noqa: E402
from backends import com_backend as module  # noqa: E402

pytestmark = pytest.mark.asyncio

_DIALOG = {
    "hwnd": 4242,
    "title": "AutoCAD - Drawing Recovery",
    "modal": True,
    "texts": ["Errors were found"],
    "buttons": [{"id": 2, "text": "Cancel"}],
}


class _Doc:
    def __init__(self, name, full, *, reject_reads=0):
        self._name, self._full, self._reject = name, full, reject_reads

    @property
    def Name(self):
        if self._reject > 0:
            self._reject -= 1
            # 1.8: pywin32's disguised rejection surfaces as RejectedLookupError
            raise module.RejectedLookupError(module._RPC_E_CALL_REJECTED, "Name")
        return self._name

    @property
    def FullName(self):
        return self._full


class _Documents:
    def __init__(self, app, *, delay=0.0, reject_reads=0):
        self._app, self._delay, self._reject = app, delay, reject_reads
        self.opened: list[str] = []
        self.Count = 0

    def Open(self, path):
        time.sleep(self._delay)
        self.opened.append(path)
        doc = _Doc("vmesni.dxf", path, reject_reads=self._reject)
        self.Count += 1
        self._app.ActiveDocument = doc
        return doc


def _backend(monkeypatch, *, delay=0.0, reject_reads=0, dialogs=()):
    app = types.SimpleNamespace(ActiveDocument=None)
    app.Documents = _Documents(app, delay=delay, reject_reads=reject_reads)
    monkeypatch.setattr(module, "_acad_app", lambda: app)
    calls = {"n": 0}
    answers = list(dialogs)

    def _modal(**_kw):
        calls["n"] += 1
        return answers.pop(0) if answers else []

    monkeypatch.setattr(module.win_dialogs, "available", lambda: True)
    monkeypatch.setattr(module.win_dialogs, "modal_dialogs", _modal)
    backend = module.ComBackend()
    backend._executor = ThreadPoolExecutor(max_workers=1)
    backend._connected = True
    backend._REJECTED_RETRY_FIRST_PAUSE_S = 0.01
    return backend, app, calls


@pytest.fixture
def timeouts(monkeypatch):
    monkeypatch.setattr(config.settings, "com_call_timeout", 15.0)
    monkeypatch.setattr(config.settings, "open_call_timeout", 120.0)
    return config.settings


def test_open_call_timeout_defaults_to_120_s(monkeypatch):
    monkeypatch.delenv("OPEN_CALL_TIMEOUT", raising=False)
    assert config.Settings().open_call_timeout == 120.0
    monkeypatch.setenv("OPEN_CALL_TIMEOUT", "45")
    assert config.Settings().open_call_timeout == 45.0


async def test_open_is_not_attempted_while_a_modal_dialog_is_shown(monkeypatch, timeouts):
    backend, app, _ = _backend(monkeypatch, dialogs=[[_DIALOG]])
    with pytest.raises(RuntimeError, match="not attempted.*Drawing Recovery.*system_dialog_close"):
        await backend.drawing_open(r"Z:\x\vmesni.dxf")
    assert app.Documents.opened == []


async def test_open_uses_open_call_timeout_not_com_call_timeout(monkeypatch, timeouts):
    timeouts.com_call_timeout = 0.3
    timeouts.open_call_timeout = 10.0
    backend, app, _ = _backend(monkeypatch, delay=0.8)
    result = await backend.drawing_open(r"Z:\x\vmesni.dxf")
    # 1.8 adds document_id / revision / external_change to the answer
    assert {k: result[k] for k in ("ok", "name", "path")} == {
        "ok": True, "name": "vmesni.dxf", "path": r"Z:\x\vmesni.dxf"
    }
    assert app.Documents.opened == [r"Z:\x\vmesni.dxf"]


async def test_a_timed_out_open_names_the_dialog_autocad_shows(monkeypatch, timeouts):
    timeouts.open_call_timeout = 0.3
    backend, _, calls = _backend(monkeypatch, delay=1.5, dialogs=[[], [_DIALOG]])
    with pytest.raises(RuntimeError) as err:
        await backend.drawing_open(r"Z:\x\vmesni.dxf")
    text = str(err.value)
    assert text.startswith("AutoCAD did not respond within")
    assert "OPEN_CALL_TIMEOUT" in text and "Drawing Recovery" in text
    assert "Errors were found" in text and calls["n"] == 2
    assert backend.is_connected is False


async def test_a_timed_out_open_without_dialog_keeps_the_plain_message(monkeypatch, timeouts):
    timeouts.open_call_timeout = 0.3
    backend, _, _ = _backend(monkeypatch, delay=1.5)
    with pytest.raises(RuntimeError, match=r"did not respond.*Increase OPEN_CALL_TIMEOUT") as err:
        await backend.drawing_open(r"Z:\x\vmesni.dxf")
    assert "AutoCAD now shows" not in str(err.value)


async def test_disguised_rejection_after_open_is_waited_out(monkeypatch, timeouts):
    backend, _, _ = _backend(monkeypatch, reject_reads=3)
    result = await backend.drawing_open(r"Z:\x\vmesni.dxf")
    assert result["name"] == "vmesni.dxf"


async def test_other_calls_keep_com_call_timeout(monkeypatch, timeouts):
    timeouts.com_call_timeout = 0.2
    backend, _, _ = _backend(monkeypatch)
    with pytest.raises(RuntimeError, match="Increase COM_CALL_TIMEOUT"):
        await backend._run(time.sleep, 1.0)