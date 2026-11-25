"""Point cloud verbs through the McpPointCloud .NET add-in (fork manydominos/Autocad-MCP).

ActiveX cannot attach an RCS/RCP point cloud and AutoCAD has no command-line
crop (``-POINTCLOUDCROP`` does not exist; ``-POINTCLOUDATTACH`` stops at its
prompts and blocks COM). The AutoCAD .NET API can: ``PointCloudEx.
AttachPointCloud``, ``addCroppingBoundary``, ``MinMaxElevation``,
``PointCloudExtractor.Extract`` (verified from the AutoCAD 2021 / R24.0
reference assemblies). The add-in in ``backends/addin/McpPointCloud.bundle``
exposes them as one AutoLISP function::

    (mcp-pc "<folder>/<id>.json")   ->  "<folder>/<id>.json.out"

The server writes the request, sends the call through ``SendCommand`` and
reads the answer file; no prompt, no dialog. This module holds the pieces
that need no AutoCAD: the request/answer files, the LISP call and the
autoloader bundle installation. The COM side is ``ComBackend._pc_call``.

Installation (``install_bundle``): the bundle is copied to
``%APPDATA%\\Autodesk\\ApplicationPlugins\\McpPointCloud.bundle``, where
AutoCAD's autoloader loads it at start-up (and when it appears, with the
default ``APPAUTOLOAD``). AutoCAD loads it without a prompt only from a
trusted location: the operator adds ``<that folder>\\Contents`` to
``TRUSTEDPATHS`` once (Options > Files > Trusted Locations). A loaded DLL is
locked until AutoCAD exits, so an update is copied on the next start.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

ADDIN_NAME = "McpPointCloud"
#: The add-in version this server speaks to (``Api.Version`` in the C#).
ADDIN_VERSION = "0.1.3"
BUNDLE_SOURCE = Path(__file__).resolve().parent / "addin" / f"{ADDIN_NAME}.bundle"

TRUSTED_PATH_HINT = (
    "AutoCAD loads the add-in without a prompt only from a trusted location: add "
    "'{contents}' to TRUSTEDPATHS once (AutoCAD Options > Files > Trusted Locations, "
    'or (setvar "TRUSTEDPATHS" ...) by the operator), then restart AutoCAD.'
)


class PointCloudAddinError(RuntimeError):
    """The add-in did not answer (not loaded) or answered with an error."""

    def __init__(self, kind: str, message: str, details: dict | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.details = details or {}


def bundle_target() -> Path:
    """Where AutoCAD's autoloader looks for per-user bundles."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Autodesk" / "ApplicationPlugins" / f"{ADDIN_NAME}.bundle"


_APP_VERSION = re.compile(r'AppVersion="([^"]+)"')


def bundle_version(bundle: Path) -> str | None:
    """``AppVersion`` of a bundle's ``PackageContents.xml``; None if unreadable."""
    try:
        text = (bundle / "PackageContents.xml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _APP_VERSION.search(text)
    return match.group(1) if match else None


def install_bundle(source: Path | None = None, target: Path | None = None) -> dict[str, Any]:
    """Copy the bundle to the autoloader folder unless the same version is there.

    Returns ``{"installed", "target", "version", "reason"}``; never raises for
    a locked DLL (an add-in AutoCAD has loaded) - that is ``reason: "in_use"``.
    """
    source = Path(source or BUNDLE_SOURCE)
    target = Path(target or bundle_target())
    wanted = bundle_version(source)
    report: dict[str, Any] = {
        "installed": False,
        "target": str(target),
        "contents": str(target / "Contents"),
        "version": wanted,
    }
    if wanted is None or not (source / "Contents" / f"{ADDIN_NAME}.dll").exists():
        report["reason"] = "bundle_missing_in_package"
        return report
    if bundle_version(target) == wanted and (target / "Contents" / f"{ADDIN_NAME}.dll").exists():
        report["reason"] = "already_installed"
        return report
    if not target.exists():
        staging = target.with_name(target.name + f".new-{uuid.uuid4().hex[:6]}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, staging)
            os.replace(staging, target)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            report["reason"] = f"copy_failed: {exc}"
            return report
        report["installed"] = True
        report["reason"] = "copied"
        return report
    # An older bundle is there, and AutoCAD may have its DLL loaded: a loaded
    # DLL cannot be overwritten or deleted, but it can be renamed. The old DLL
    # becomes McpPointCloud.dll.old-<id> (removed at a later install), the new
    # one takes its name and AutoCAD loads it at its next start.
    staged = False
    try:
        for leftover in (target / "Contents").glob(f"{ADDIN_NAME}.dll.old-*"):
            try:
                leftover.unlink()
            except OSError:
                pass  # still loaded; removed next time
        for src in sorted(p for p in source.rglob("*") if p.is_file()):
            dst = target / src.relative_to(source)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.suffix.lower() == ".xml":
                continue  # the version file last, once the DLL is in place
            try:
                shutil.copyfile(src, dst)
            except PermissionError:
                os.replace(dst, dst.with_name(dst.name + f".old-{uuid.uuid4().hex[:6]}"))
                shutil.copyfile(src, dst)
                staged = True
        shutil.copyfile(source / "PackageContents.xml", target / "PackageContents.xml")
    except OSError as exc:
        report["reason"] = f"copy_failed: {exc}"
        return report
    report["installed"] = True
    report["reason"] = "staged_restart_autocad" if staged else "copied"
    return report


def request_dir() -> Path:
    folder = Path(tempfile.gettempdir()) / "autocad-mcp-pc"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_request(op: str, payload: dict[str, Any], folder: Path | None = None) -> Path:
    """Write ``{"op": op, **payload}`` as a request file; returns its path."""
    folder = Path(folder or request_dir())
    path = folder / f"{uuid.uuid4().hex}.json"
    body = {"op": op, **{k: v for k, v in payload.items() if v is not None}}
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def answer_path(request: Path) -> Path:
    return request.with_name(request.name + ".out")


def read_answer(request: Path) -> dict | None:
    """The add-in's answer, or None while it is not written yet."""
    out = answer_path(request)
    if not out.exists():
        return None
    try:
        return json.loads(out.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None  # still being moved into place; read again


def cleanup(request: Path) -> None:
    for path in (request, answer_path(request), request.with_name(request.name + ".out.part")):
        try:
            path.unlink()
        except OSError:
            pass


def lisp_call(request: Path) -> str:
    """The AutoLISP form that runs one request (forward slashes, no quotes in the path)."""
    text = str(request).replace("\\", "/")
    if '"' in text:
        raise ValueError(f"request path must not contain quotes: {text!r}")
    return f'(mcp-pc "{text}")'


def not_loaded_error(install: dict[str, Any] | None) -> PointCloudAddinError:
    contents = (install or {}).get("contents") or str(bundle_target() / "Contents")
    state = ""
    if install:
        state = f" Bundle: {install.get('reason')} at {install.get('target')}."
    return PointCloudAddinError(
        "addin_not_loaded",
        "the McpPointCloud add-in did not answer in AutoCAD (mcp-pc is not defined)."
        + state
        + " "
        + TRUSTED_PATH_HINT.format(contents=contents),
        {"install": install},
    )
