"""drawing_save / document_close on a network share never rename a file AutoCAD holds open.

Fork branch popravek/shranjevanje-mreza. Twice on AutoCAD 2021 (P2026-018/019)
Save over an open DWG on a VMware shared folder lost the DWG and left a locked
.bak and tmp files. SaveAs to a new name on the same share always worked, so
the network save is built from that only.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import config

pytestmark = pytest.mark.asyncio


class _Doc:
    def __init__(self, path: Path, *, fail_on=None, isavebak=1):
        self.FullName = str(path)
        self.Name = path.name
        self.Saved = False
        self.calls = []
        self.vars = {"ISAVEBAK": isavebak}
        self.fail_on = fail_on
        self.closed = None

    def GetVariable(self, name):
        return self.vars[name]

    def SaveAs(self, path, code=None):
        self.calls.append(("SaveAs", Path(path).name, code))
        if self.fail_on and Path(path).name == self.fail_on:
            raise RuntimeError("Error saving the document")
        Path(path).write_bytes(b"AC1032" + b"\x00" * 64)
        self.FullName = str(path)
        self.Name = Path(path).name

    def Save(self):
        self.calls.append(("Save",))

    def Close(self, save):
        self.calls.append(("Close", save))
        self.closed = save


@pytest.fixture
def seat(monkeypatch, tmp_path):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    import backends.com_backend as cb

    target = tmp_path / "plan.dwg"
    target.write_bytes(b"AC1027" + b"\x00" * 64)  # an R2013 drawing
    holder = types.SimpleNamespace(doc=_Doc(target))
    app = types.SimpleNamespace(Documents=types.SimpleNamespace(Count=0), ActiveDocument=None)
    monkeypatch.setattr(cb, "_acad_doc", lambda: holder.doc)
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "_is_network_path", lambda p: True)
    monkeypatch.setattr(config.settings, "network_save_mode", "safe")
    backend = cb.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _no_lineage(*a, **k):
        return None

    monkeypatch.setattr(backend, "_run", _run_inline)
    monkeypatch.setattr(backend, "_record_lineage", _no_lineage)
    return backend, holder, target


async def test_save_on_a_share_goes_through_a_side_name(seat):
    backend, holder, target = seat
    result = await backend.drawing_save()
    assert result["method"] == "network_safe"
    assert holder.doc.calls == [
        ("SaveAs", "plan.mcp-save.dwg", 60),  # R2013 kept from the header
        ("SaveAs", "plan.dwg", 60),
    ], "never Save() over the open file"
    assert target.exists() and target.with_suffix(".bak").exists()
    assert not (target.parent / "plan.mcp-save.dwg").exists()
    assert holder.doc.FullName == str(target)
    assert result["files"]["dwg"] is True and result["files"]["mcp_save"] is False


async def test_isavebak_zero_leaves_no_bak(seat):
    backend, holder, target = seat
    holder.doc.vars["ISAVEBAK"] = 0
    await backend.drawing_save()
    assert target.exists() and not target.with_suffix(".bak").exists()


async def test_a_failed_final_save_keeps_the_side_copy_and_names_the_files(seat):
    backend, holder, target = seat
    holder.doc.fail_on = "plan.dwg"
    with pytest.raises(RuntimeError) as info:
        await backend.drawing_save()
    part = target.parent / "plan.mcp-save.dwg"
    assert part.exists(), "the drawing survives in the side copy"
    assert target.with_suffix(".bak").exists(), "and the previous version as .bak"
    assert "mcp_save" in str(info.value)


async def test_a_failed_first_save_touches_nothing(seat):
    backend, holder, target = seat
    holder.doc.fail_on = "plan.mcp-save.dwg"
    original = target.read_bytes()
    with pytest.raises(RuntimeError):
        await backend.drawing_save()
    assert target.read_bytes() == original
    assert not target.with_suffix(".bak").exists()


async def test_direct_mode_is_upstreams_save(seat, monkeypatch):
    backend, holder, _target = seat
    monkeypatch.setattr(config.settings, "network_save_mode", "direct")
    result = await backend.drawing_save()
    assert holder.doc.calls == [("Save",)] and "method" not in result


async def test_local_paths_keep_save(seat, monkeypatch):
    import backends.com_backend as cb

    backend, holder, _target = seat
    monkeypatch.setattr(cb, "_is_network_path", lambda p: False)
    await backend.drawing_save()
    assert holder.doc.calls == [("Save",)]


async def test_a_new_file_on_a_share_is_a_plain_save_as(seat):
    backend, holder, target = seat
    new = target.parent / "other.dwg"
    await backend.drawing_save(str(new))
    assert holder.doc.calls == [("SaveAs", "other.dwg", None)]


async def test_close_with_save_on_a_share_saves_safely_then_closes_clean(seat):
    backend, holder, target = seat
    result = await backend.document_close(save=True)
    assert holder.doc.calls[-1] == ("Close", False)
    assert ("SaveAs", "plan.mcp-save.dwg", 60) in holder.doc.calls
    assert target.exists() and result["saved"] is True


async def test_unc_and_remote_drive_detection(monkeypatch):
    import backends.com_backend as cb

    assert cb._is_network_path("\\\\vmware-host\\Shared Folders\\x.dwg") is True
    assert cb._is_network_path("//server/share/x.dwg") is True
    assert cb._is_network_path("relative.dwg") is False
