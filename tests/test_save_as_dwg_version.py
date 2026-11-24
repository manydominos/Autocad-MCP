"""drawing_save_as writes a DWG in a chosen release (default R2018), not R2000.

Until this change ``.dwg`` mapped to AcSaveAsType 12 (ac2000_dwg, header
AC1015): AutoCAD 2021 then dropped what R2000 cannot store, an attached
point cloud among it. The code now comes from ``_DWG_SAVE_AS`` - the table
``drawing_export_dwg`` already used - and the header is read back.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import server

pytestmark = pytest.mark.asyncio

_HEADER = {12: b"AC1015", 24: b"AC1018", 36: b"AC1021", 48: b"AC1024", 60: b"AC1027", 64: b"AC1032"}


class _Doc:
    def __init__(self):
        self.calls = []
        self.Name = "Drawing1.dwg"
        self.SummaryInfo = types.SimpleNamespace()

    def SaveAs(self, path, fmt=None):
        self.calls.append((path, fmt))
        self.Name = Path(path).name
        Path(path).write_bytes(_HEADER.get(fmt, b"AC1032") + b"\x00" * 32)


@pytest.fixture
def com(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    from backends import com_backend as module

    doc = _Doc()
    monkeypatch.setattr(module, "_acad_doc", lambda: doc)
    backend = module.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, doc


async def test_dwg_defaults_to_r2018(com, tmp_path):
    backend, doc = com
    out = str(tmp_path / "a.dwg")
    result = await backend.drawing_save_as(out, "dwg")
    assert doc.calls == [(out, 64)]
    assert result["version"] == "R2018"
    assert result["file_version"] == {"code": "AC1032", "release": "R2018"}


@pytest.mark.parametrize(
    "version,code", [("R2000", 12), ("R2004", 24), ("R2007", 36), ("R2010", 48), ("R2013", 60)]
)
async def test_dwg_version_uses_the_export_table(com, tmp_path, version, code):
    backend, doc = com
    out = str(tmp_path / "b.dwg")
    result = await backend.drawing_save_as(out, "dwg", version=version)
    assert doc.calls == [(out, code)]
    assert result["version"] == version
    assert result["file_version"]["release"] == version
    assert backend._DWG_SAVE_AS[version] == code


async def test_unknown_version_is_refused_before_saving(com, tmp_path):
    backend, doc = com
    with pytest.raises(ValueError, match="R2018"):
        await backend.drawing_save_as(str(tmp_path / "c.dwg"), "dwg", version="2021")
    assert doc.calls == []


async def test_version_on_dxf_is_refused(com, tmp_path):
    backend, doc = com
    with pytest.raises(ValueError, match="DWG only"):
        await backend.drawing_save_as(str(tmp_path / "c.dxf"), "dxf", version="R2018")
    assert doc.calls == []


async def test_dxf_and_dwt_codes_unchanged(com, tmp_path):
    backend, doc = com
    await backend.drawing_save_as(str(tmp_path / "d.dxf"), "dxf")
    await backend.drawing_save_as(str(tmp_path / "e.dwt"), "dwt")
    assert [c[1] for c in doc.calls] == [61, 66]


async def test_header_reader_unknown_is_none(tmp_path):
    from backends.com_backend import dwg_header_version

    assert dwg_header_version(tmp_path / "missing.dwg") is None
    odd = tmp_path / "odd.dwg"
    odd.write_bytes(b"AC1099xx")
    assert dwg_header_version(odd) == {"code": "AC1099", "release": None}


class _Ctx:
    def __init__(self, backend):
        self.backend = backend

    async def info(self, *_a, **_k):
        pass


async def test_tool_passes_version_and_maps_bad_value(monkeypatch, tmp_path):
    seen = {}

    class _Backend:
        async def drawing_save_as(self, path, fmt, version=None):
            seen["args"] = (fmt, version)
            if version == "bad":
                raise ValueError("DWG version must be one of ...")
            return {"ok": True}

    backend = _Backend()
    monkeypatch.setattr(server, "_backend", lambda ctx: backend)
    monkeypatch.setattr(server, "validate_path", lambda p, **k: p)
    await server.drawing_save_as(str(tmp_path / "x.dwg"), version="R2013", ctx=_Ctx(backend))
    assert seen["args"] == ("dwg", "R2013")
    with pytest.raises(ToolError, match="invalid_args"):
        await server.drawing_save_as(str(tmp_path / "x.dwg"), version="bad", ctx=_Ctx(backend))
