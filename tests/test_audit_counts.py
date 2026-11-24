"""drawing_audit waits for AUDIT and reads the counts from the .adt log.

Fork branch popravek/audit-stevci. On AutoCAD 2021 drawing_audit returned
while AUDIT was still queued (counts null, P2026-018) and a drawing_save
right behind it lost the DWG on a VMware share (P2026-019).
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio

_ADT = """Auditing Header

Auditing Tables

Auditing Entities Pass 1

Pass 1 1200    objects audited
Auditing Entities Pass 2

Pass 2 1200    objects audited
Auditing Blocks

 12     Blocks audited

Total errors found 3 fixed 2

Erased 1 objects
"""


class _AuditDoc:
    def __init__(self, folder, *, writes_log=True, busy_polls=2, log_text=_ADT):
        self.variables = {"CMDACTIVE": 0, "AUDITCTL": 0}
        self.FullName = str(folder / "plan.dwg") if folder else ""
        self.calls = []
        self._writes_log = writes_log
        self._busy = 0
        self._busy_polls = busy_polls
        self._log_text = log_text

    def GetVariable(self, name):
        self.calls.append(("GetVariable", name))
        if name == "CMDACTIVE" and self._busy:
            self._busy -= 1
            if not self._busy and self._writes_log and self.FullName:
                path = self.FullName[:-4] + ".adt"
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(self._log_text)
                later = time.time() + 5
                os.utime(path, (later, later))
            return 1
        return self.variables[name]

    def SetVariable(self, name, value):
        self.calls.append(("SetVariable", name, value))
        self.variables[name] = value

    def SendCommand(self, cmd):
        self.calls.append(("SendCommand", cmd))
        self._busy = self._busy_polls


@pytest.fixture
def seat(monkeypatch, tmp_path):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    import backends.com_backend as cb

    holder = SimpleNamespace(doc=_AuditDoc(tmp_path))
    monkeypatch.setattr(cb, "_acad_doc", lambda: holder.doc)
    backend = cb.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, holder, tmp_path


async def test_counts_come_from_the_adt_after_audit_finished(seat):
    backend, holder, tmp_path = seat
    result = await backend.drawing_audit()
    assert result["completed"] is True and result["detail"] == "adt"
    assert result["error_count"] == 3 and result["fix_count"] == 2
    assert result["erased_count"] == 1 and result["repaired"] is True
    assert result["log_path"] == str(tmp_path / "plan.adt")
    polls = [c for c in holder.doc.calls if c == ("GetVariable", "CMDACTIVE")]
    assert len(polls) >= 3, "waited while CMDACTIVE was 1"
    assert holder.doc.variables["AUDITCTL"] == 0, "the operator's AUDITCTL is back"


async def test_a_stale_log_is_not_read_as_this_run(seat):
    backend, holder, tmp_path = seat
    stale = tmp_path / "plan.adt"
    stale.write_text("Total errors found 9 fixed 9\n", encoding="utf-8")
    holder.doc = _AuditDoc(tmp_path, writes_log=False)
    result = await backend.drawing_audit()
    assert result["error_count"] is None and result["detail"] == "unavailable"
    assert result["completed"] is True


async def test_unsaved_drawing_has_no_counts(seat):
    backend, holder, _tmp = seat
    holder.doc = _AuditDoc(None)
    result = await backend.drawing_audit()
    assert result["fix_count"] is None and result["log_path"] is None
    assert "unknown, not zero" in result["message"]


async def test_busy_autocad_is_refused_before_audit(seat):
    backend, holder, _tmp = seat
    holder.doc.variables["CMDACTIVE"] = 1
    with pytest.raises(RuntimeError, match="CMDACTIVE"):
        await backend.drawing_audit()
    assert not any(c[0] == "SendCommand" for c in holder.doc.calls)


async def test_audit_still_running_at_the_deadline(seat, monkeypatch):
    import backends.com_backend as cb

    backend, holder, tmp_path = seat
    holder.doc = _AuditDoc(tmp_path, busy_polls=10**9)
    monkeypatch.setattr(cb, "_audit_deadline_s", lambda: 0.1)
    result = await backend.drawing_audit()
    assert result["completed"] is False and result["error_count"] is None
    assert holder.doc.variables["AUDITCTL"] == 0


async def test_parse_audit_log_takes_the_last_run(tmp_path):
    from backends.com_backend import parse_audit_log

    log = tmp_path / "x.adt"
    log.write_text(_ADT + "\nTotal errors found 0 fixed 0\nErased 0 objects\n", encoding="utf-8")
    assert parse_audit_log(log) == {"error_count": 0, "fix_count": 0, "erased": 0}
    other = tmp_path / "y.adt"
    other.write_text("Gesamtzahl gefundener Fehler 1\n", encoding="utf-8")
    assert parse_audit_log(other) is None
    assert parse_audit_log(tmp_path / "missing.adt") is None


async def test_deadline_stays_inside_the_com_timeout(monkeypatch):
    import backends.com_backend as cb
    import config

    monkeypatch.setattr(config.settings, "com_call_timeout", 15.0)
    monkeypatch.setattr(config.settings, "command_deadline_s", 8.0)
    assert cb._audit_deadline_s() == 13.0
