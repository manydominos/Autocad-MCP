"""view_capture: the AutoCAD view window with its drawing scale (fork, popravek/view-capture).

view_render(region) draws an exported DXF, so an attached point cloud was never
on the picture (P2026-018); the window capture had no scale and the ribbon.
"""

from __future__ import annotations

import io
import json
import types

import pytest

pytestmark = pytest.mark.asyncio


def _png(width, height):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


async def test_report_maps_pixels_to_world():
    from backends.com_backend import capture_report

    frame = {
        "view_center": [5000.0, 2000.0],
        "view_height": 4000.0,
        "screen_px": [1600, 800],
        "plan_view": True,
        "world_ucs": True,
    }
    report = capture_report(frame, (1600, 800))
    assert report["px_per_unit"] == pytest.approx(0.2)  # 800 px over 4000 mm
    assert report["world_window"] == pytest.approx([1000.0, 0.0, 9000.0, 4000.0])
    half = capture_report(frame, (800, 400), factor=0.5)
    assert half["px_per_unit"] == pytest.approx(0.1)
    assert half["world_window"] == pytest.approx([1000.0, 0.0, 9000.0, 4000.0])


@pytest.mark.parametrize(
    "change",
    [{"plan_view": False}, {"world_ucs": False}, {"screen_px": [1000, 800]}, {"view_height": None}],
)
async def test_no_scale_unless_plan_world_and_matching(change):
    from backends.com_backend import capture_report

    frame = {
        "view_center": [0.0, 0.0],
        "view_height": 100.0,
        "screen_px": [1600, 800],
        "plan_view": True,
        "world_ucs": True,
        **change,
    }
    report = capture_report(frame, (1600, 800))
    assert report["px_per_unit"] is None and report["world_window"] is None
    assert "no scale" in report["note"]


class _Doc:
    def __init__(self):
        self.vars = {
            "VIEWCTR": (500.0, 250.0, 0.0),
            "VIEWSIZE": 500.0,
            "SCREENSIZE": (1000.0, 500.0),
            "VIEWDIR": (0.0, 0.0, 1.0),
            "VIEWTWIST": 0.0,
            "WORLDUCS": 1,
            "NAVVCUBEDISPLAY": 3,
            "NAVBARDISPLAY": 1,
            "UCSICON": 3,
            "GRIDMODE": 1,
        }
        self.sets = []

    def GetVariable(self, name):
        return self.vars[name]

    def SetVariable(self, name, value):
        self.sets.append((name, value))
        self.vars[name] = value


@pytest.fixture
def live(monkeypatch):
    pytest.importorskip("win32com.client", reason="pywin32 not installed")
    import backends.com_backend as cb

    doc = _Doc()
    zooms = []
    app = types.SimpleNamespace(ZoomWindow=lambda a, b: zooms.append((a, b)), Update=lambda: None)
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    monkeypatch.setattr(cb, "_acad_app", lambda: app)
    monkeypatch.setattr(cb, "_apoint", lambda x, y: (x, y))
    monkeypatch.setattr(cb, "_find_autocad_hwnd", lambda: 100)
    monkeypatch.setattr(cb, "_find_view_hwnd", lambda main, px: 200)
    monkeypatch.setattr(cb, "_capture_window", lambda hwnd: _png(1000, 500))
    backend = cb.ComBackend()

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(backend, "_run", _run_inline)
    return backend, doc, zooms


async def test_capture_zooms_hides_ui_restores_and_scales(live, tmp_path):
    backend, doc, zooms = live
    out = tmp_path / "shot.png"
    report = await backend.view_capture(
        window=[0, 0, 1000, 500], path=str(out), max_px=4096, settle_s=0
    )
    assert zooms == [((0, 0), (1000, 500))]
    assert ("NAVVCUBEDISPLAY", 0) in doc.sets and ("GRIDMODE", 0) in doc.sets
    assert doc.vars["NAVVCUBEDISPLAY"] == 3 and doc.vars["GRIDMODE"] == 1, "restored"
    assert report["px"] == [1000, 500] and report["px_per_unit"] == pytest.approx(1.0)
    assert report["world_window"] == pytest.approx([0.0, 0.0, 1000.0, 500.0])
    assert out.read_bytes()[:4] == b"\x89PNG"
    meta = json.loads((tmp_path / "shot.png.json").read_text(encoding="utf-8"))
    assert meta["px_per_unit"] == pytest.approx(1.0) and "png" not in meta


async def test_downscale_keeps_the_world_window(live):
    backend, _doc, _z = live
    report = await backend.view_capture(max_px=500, settle_s=0)
    assert report["px"] == [500, 250]
    assert report["px_per_unit"] == pytest.approx(0.5)
    assert report["world_window"] == pytest.approx([0.0, 0.0, 1000.0, 500.0])


async def test_no_view_window_is_reported(live, monkeypatch):
    import backends.com_backend as cb

    backend, doc, _z = live
    monkeypatch.setattr(cb, "_find_view_hwnd", lambda main, px: None)
    report = await backend.view_capture(settle_s=0)
    assert report["png"] is None and report["window_found"] is False
    assert doc.vars["UCSICON"] == 3


@pytest.fixture
def styled(live, monkeypatch):
    import backends.com_backend as cb

    backend, doc, _z = live
    doc.vars["VSCURRENT"] = "2dWireframe"
    sent = []

    def _send(d, cmd, *a, **k):
        sent.append(cmd)
        d.vars["VSCURRENT"] = {"_Hidden": "Hidden", "_2dwireframe": "2dWireframe"}.get(
            cmd.split()[-1], cmd.split()[-1]
        )

    monkeypatch.setattr(cb.ComBackend, "_safe_send_command", staticmethod(_send))
    return backend, doc, sent


async def test_a_cloud_in_2d_wireframe_is_shot_in_hidden_and_restored(styled, monkeypatch):
    import backends.com_backend as cb

    backend, doc, sent = styled
    monkeypatch.setattr(cb, "_has_point_cloud", lambda d: True)
    report = await backend.view_capture(settle_s=0)
    assert sent == ["_.VSCURRENT _Hidden", "_.VSCURRENT _2dwireframe"]
    assert report["visual_style"] == {
        "previous": "2dWireframe",
        "point_cloud_found": True,
        "used": "Hidden",
        "restored": True,
    }
    assert doc.vars["VSCURRENT"] == "2dWireframe"


async def test_no_cloud_or_keep_leaves_the_style(styled, monkeypatch):
    import backends.com_backend as cb

    backend, _doc, sent = styled
    monkeypatch.setattr(cb, "_has_point_cloud", lambda d: False)
    await backend.view_capture(settle_s=0)
    monkeypatch.setattr(cb, "_has_point_cloud", lambda d: True)
    report = await backend.view_capture(settle_s=0, visual_style="keep")
    assert sent == [] and report["visual_style"]["used"] == "2dWireframe"


async def test_an_unknown_visual_style_is_refused(styled):
    backend, _doc, sent = styled
    with pytest.raises(ValueError, match="visual_style"):
        await backend.view_capture(settle_s=0, visual_style="sketchy")
    assert sent == []


async def test_headless_refuses_by_capability(tmp_path):
    from backends.capability import UnsupportedCapabilityError
    from backends.ezdxf_backend import EzdxfBackend

    with pytest.raises(UnsupportedCapabilityError) as info:
        await EzdxfBackend().view_capture()
    assert info.value.capability == "view_capture"


async def test_unreadable_vscurrent_is_taken_as_2d_wireframe(styled, monkeypatch):
    """Retest 2026-11-25: VSCURRENT is not readable on AutoCAD 2021, so nothing switched."""
    import backends.com_backend as cb

    backend, doc, sent = styled
    del doc.vars["VSCURRENT"]

    def _send(d, cmd, *a, **k):
        sent.append(cmd)

    monkeypatch.setattr(cb.ComBackend, "_safe_send_command", staticmethod(_send))
    monkeypatch.setattr(cb, "_has_point_cloud", lambda d: True)
    report = await backend.view_capture(settle_s=0)
    assert sent == ["_.VSCURRENT _Hidden", "_.VSCURRENT _2dwireframe"]
    style = report["visual_style"]
    assert style["previous"] is None and style["previous_assumed"] == "2D Wireframe"
    assert style["used"] == "hidden" and style["restored"] is True


async def test_the_settle_wait_is_outside_the_com_jobs(live, monkeypatch):
    """Retest 2026-11-25: settle_s 20 inside one COM job overran COM_CALL_TIMEOUT."""
    import backends.com_backend as cb

    backend, _doc, _z = live
    jobs, waits = [], []

    async def _run_counting(func, *args, **kwargs):
        jobs.append(func.__name__)
        return func(*args, **kwargs)

    async def _sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(backend, "_run", _run_counting)
    monkeypatch.setattr(cb.asyncio, "sleep", _sleep)
    await backend.view_capture(settle_s=20)
    assert jobs == ["_prepare", "_shoot", "_restore"] and waits == [20.0]
