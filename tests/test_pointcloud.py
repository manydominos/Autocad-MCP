"""Point cloud verbs through the McpPointCloud add-in (fork, popravek/oblak-tock).

AutoCAD itself is not here: the add-in is faked by a document whose
SendCommand reads the request file and writes the answer file, exactly the
protocol backends/pointcloud.py and the C# Api.Dispatch share.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import config
import server
from backends import pointcloud as pc

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[1]


# ── the shipped add-in ───────────────────────────────────────────────────────


async def test_bundle_is_shipped_with_matching_versions():
    bundle = pc.BUNDLE_SOURCE
    assert (bundle / "Contents" / "McpPointCloud.dll").stat().st_size > 10_000
    assert pc.bundle_version(bundle) == pc.ADDIN_VERSION
    source = (ROOT / "addin" / "McpPointCloud" / "McpPointCloud.cs").read_text(encoding="utf-8")
    assert re.search(r'Version = "([^"]+)"', source).group(1) == pc.ADDIN_VERSION
    assert '[LispFunction("mcp-pc")]' in source


async def test_no_autodesk_assembly_is_committed():
    shipped = {p.name.lower() for p in (pc.BUNDLE_SOURCE / "Contents").iterdir()}
    assert shipped == {"mcpcloud.dll".replace("cloud", "pointcloud")}
    assert not (ROOT / "addin" / "McpPointCloud" / "_ref").exists()


# ── files and the LISP call ──────────────────────────────────────────────────


async def test_request_answer_round_trip(tmp_path):
    request = pc.write_request("crop", {"handle": "2A", "zmin": None}, folder=tmp_path)
    body = json.loads(request.read_text(encoding="utf-8"))
    assert body == {"op": "crop", "handle": "2A"}, "None values are not sent"
    assert pc.read_answer(request) is None
    pc.answer_path(request).write_text('{"ok": true, "crops": 1}', encoding="utf-8")
    assert pc.read_answer(request) == {"ok": True, "crops": 1}
    pc.cleanup(request)
    assert not request.exists() and not pc.answer_path(request).exists()


async def test_half_written_answer_reads_as_not_yet(tmp_path):
    request = pc.write_request("info", {}, folder=tmp_path)
    pc.answer_path(request).write_text('{"ok": tr', encoding="utf-8")
    assert pc.read_answer(request) is None


async def test_lisp_call_uses_forward_slashes_and_refuses_quotes():
    assert pc.lisp_call(Path("C:\\Temp\\a b\\x.json")) == '(mcp-pc "C:/Temp/a b/x.json")'
    with pytest.raises(ValueError):
        pc.lisp_call(Path('C:/x"y.json'))


# ── bundle installation ──────────────────────────────────────────────────────


async def test_install_copies_then_skips_the_same_version(tmp_path):
    target = tmp_path / "ApplicationPlugins" / "McpPointCloud.bundle"
    first = pc.install_bundle(target=target)
    assert first["installed"] is True and (target / "Contents" / "McpPointCloud.dll").exists()
    again = pc.install_bundle(target=target)
    assert again["installed"] is False and again["reason"] == "already_installed"


async def test_install_replaces_an_older_version(tmp_path):
    target = tmp_path / "McpPointCloud.bundle"
    (target / "Contents").mkdir(parents=True)
    (target / "PackageContents.xml").write_text('<x AppVersion="0.0.1"/>', encoding="utf-8")
    report = pc.install_bundle(target=target)
    assert report["installed"] is True and pc.bundle_version(target) == pc.ADDIN_VERSION
    assert not list(tmp_path.glob("*.old-*")) and not list(tmp_path.glob("*.new-*"))


async def test_not_loaded_error_names_the_folder_to_trust(tmp_path):
    error = pc.not_loaded_error({"contents": "C:/X/Contents", "reason": "copied", "target": "C:/X"})
    text = str(error)
    assert (
        text.startswith("addin_not_loaded:") and "TRUSTEDPATHS" in text and "C:/X/Contents" in text
    )


# ── ComBackend._pc_call against a fake add-in ────────────────────────────────


class _Doc:
    def __init__(self, handler=None):
        self.vars = {"CMDACTIVE": 0}
        self.sent = []
        self.handler = handler

    def GetVariable(self, name):
        return self.vars[name]

    def SetVariable(self, name, value):
        self.vars[name] = value

    def SendCommand(self, cmd):
        self.sent.append(cmd)
        if self.handler is None:
            return  # add-in not loaded: AutoLISP says "no function definition"
        match = re.fullmatch(r'\(mcp-pc "(.+)"\)\n', cmd)
        request = Path(match.group(1))
        body = json.loads(request.read_text(encoding="utf-8"))
        pc.answer_path(request).write_text(json.dumps(self.handler(body)), encoding="utf-8")


@pytest.fixture
def live(monkeypatch, tmp_path):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    import backends.com_backend as cb

    holder = types.SimpleNamespace(doc=_Doc(lambda body: {"ok": True, "echo": body}))
    monkeypatch.setattr(cb, "_acad_doc", lambda: holder.doc)
    monkeypatch.setattr(pc, "request_dir", lambda: tmp_path)
    backend = cb.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _run_timed_inline(timeout, setting, func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    monkeypatch.setattr(backend, "_run_timed", _run_timed_inline)
    return backend, holder, tmp_path


async def test_a_call_round_trips_and_cleans_up(live):
    backend, holder, folder = live
    answer = await backend.pointcloud("crop", {"handle": "1F", "polygon": [[0, 0], [1, 0], [1, 1]]})
    assert answer["ok"] is True and answer["echo"]["op"] == "crop"
    assert answer["echo"]["handle"] == "1F"
    assert holder.doc.sent[0].startswith('(mcp-pc "')
    assert list(folder.iterdir()) == [], "request and answer files removed"


async def test_an_add_in_error_is_typed(live):
    backend, holder, _f = live
    holder.doc.handler = lambda body: {"ok": False, "kind": "not_found", "error": "no entity 99"}
    with pytest.raises(ToolError, match="^not_found: no entity 99"):
        await backend.pointcloud("info", {"handle": "99"})


async def test_no_answer_installs_the_bundle_and_says_how_to_trust_it(live, monkeypatch):
    backend, holder, _f = live
    holder.doc.handler = None
    calls = []

    def _install():
        calls.append(1)
        return {"installed": True, "reason": "copied", "target": "T", "contents": "T/Contents"}

    monkeypatch.setattr(pc, "install_bundle", _install)
    monkeypatch.setattr(config.settings, "pointcloud_addin_autoinstall", True)
    monkeypatch.setattr("time.sleep", lambda s: None)
    clock = iter(range(0, 10_000))
    monkeypatch.setattr("time.monotonic", lambda: float(next(clock)))
    with pytest.raises(ToolError, match="addin_not_loaded.*TRUSTEDPATHS"):
        await backend.pointcloud("ping", {})
    assert calls == [1]


async def test_autoinstall_off_does_not_copy(live, monkeypatch):
    backend, holder, _f = live
    holder.doc.handler = None
    monkeypatch.setattr(pc, "install_bundle", lambda: pytest.fail("must not install"))
    monkeypatch.setattr(config.settings, "pointcloud_addin_autoinstall", False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    clock = iter(range(0, 10_000))
    monkeypatch.setattr("time.monotonic", lambda: float(next(clock)))
    with pytest.raises(ToolError, match="addin_not_loaded"):
        await backend.pointcloud("ping", {})


async def test_busy_autocad_sends_nothing(live):
    backend, holder, _f = live
    holder.doc.vars["CMDACTIVE"] = 1
    with pytest.raises(RuntimeError, match="CMDACTIVE"):
        await backend.pointcloud("info", {})
    assert holder.doc.sent == []


async def test_style_sets_the_display_sysvars_after_the_add_in(live, monkeypatch):
    import backends.com_backend as cb

    backend, holder, _f = live
    seen = []
    monkeypatch.setattr(cb, "_set_variable", lambda doc, name, value: seen.append((name, value)))
    answer = await backend.pointcloud("style", {"stylization": "height", "point_size": 3, "lod": 8})
    assert "point_size" not in answer["echo"] and answer["echo"]["stylization"] == "height"
    assert seen == [("POINTCLOUDPOINTSIZE", 3), ("POINTCLOUDLOD", 8)]
    assert answer["sysvars"] == {"POINTCLOUDPOINTSIZE": 3, "POINTCLOUDLOD": 8}


async def test_headless_refuses_by_capability():
    from backends.capability import UnsupportedCapabilityError
    from backends.ezdxf_backend import EzdxfBackend

    with pytest.raises(UnsupportedCapabilityError) as info:
        await EzdxfBackend().pointcloud("info", {})
    assert info.value.capability == "pointcloud"


# ── the tools' own validation ────────────────────────────────────────────────


class _Ctx:
    async def info(self, *_a, **_k):
        pass


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    class _Backend:
        async def pointcloud(self, op, payload):
            calls.append((op, payload))
            return {"ok": True}

    backend = _Backend()
    monkeypatch.setattr(server, "_backend", lambda ctx: backend)
    monkeypatch.setattr(server, "validate_path", lambda p, **k: Path(p))
    return calls


async def test_attach_payload_and_extension(recorder):
    await server.pointcloud_attach("Z:/scans/site.rcs", scale=1000, layer="00_PCL", ctx=_Ctx())
    op, payload = recorder[-1]
    assert op == "attach" and payload["insert"] == [0.0, 0.0, 0.0] and payload["scale"] == 1000.0
    assert payload["layer"] == "00_PCL"
    with pytest.raises(ToolError, match="rcs or .rcp"):
        await server.pointcloud_attach("Z:/scans/site.e57", scale=1000, ctx=_Ctx())


async def test_crop_validation_and_payload(recorder):
    square = [[0, 0], [10, 0], [10, 10], [0, 10]]
    await server.pointcloud_crop(square, zmin=950, zmax=1050, ctx=_Ctx())
    op, payload = recorder[-1]
    assert op == "crop" and payload["zmin"] == 950 and payload["zband_mode"] == "elevation"
    for bad in (
        {"polygon": square[:2]},
        {"polygon": square, "plane": "iso"},
        {"polygon": square, "zmin": 1},
        {"polygon": square, "zmin": 5, "zmax": 1},
        {"polygon": square, "cloud": "xyz"},
    ):
        with pytest.raises(ToolError, match="invalid_args"):
            await server.pointcloud_crop(ctx=_Ctx(), **bad)


async def test_style_validation(recorder):
    with pytest.raises(ToolError, match="stylization"):
        await server.pointcloud_style(stylization="rainbow", ctx=_Ctx())
    with pytest.raises(ToolError, match="lod"):
        await server.pointcloud_style(lod=11, ctx=_Ctx())
    await server.pointcloud_style(stylization="height", elevation=[0, 3000], ctx=_Ctx())
    assert recorder[-1][1]["elevation"] == [0, 3000]


async def test_section_defaults_to_a_plan_cut(recorder):
    await server.pointcloud_section([0, 0, 1000], ctx=_Ctx())
    op, payload = recorder[-1]
    assert op == "section" and payload["normal"] == [0.0, 0.0, 1.0]
    assert payload["layer"] == "KONTROLA_OBLAK"
