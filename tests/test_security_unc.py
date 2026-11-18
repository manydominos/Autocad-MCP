r"""UNC roots named in ALLOWED_PATHS (agent mcp, v2).

A mapped network drive (Z: on a VMware shared folder) resolves to
``\\vmware-host\Shared Folders\...``. v1 refused every UNC anchor before
ALLOWED_PATHS was consulted, so no drawing could be saved or opened on Z:.
v2 accepts a UNC path only inside a UNC root that ALLOWED_PATHS names; every
other non-local anchor stays refused.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import config
import security
from security import validate_path

pytestmark = pytest.mark.skipif(os.name != "nt", reason="UNC anchors are a Windows concept")

SHARE = r"\\vmware-host\Shared Folders\100_AGENTS-WORK\AGENT-TEST"


@pytest.fixture
def allowed(monkeypatch):
    """Set ALLOWED_PATHS roots directly (no resolve, no network)."""

    def _set(*roots: str) -> None:
        monkeypatch.setattr(config.settings, "allowed_paths", [Path(r) for r in roots])

    return _set


@pytest.fixture
def allowed_env(monkeypatch):
    """Set ALLOWED_PATHS the way the server does: env var parsed by Settings()."""

    def _set(value: str) -> None:
        monkeypatch.setenv("ALLOWED_PATHS", value)
        monkeypatch.setattr(config, "settings", config.Settings())

    return _set


def _inside(path: str) -> bool:
    return security._unc_inside_allowed_root(Path(path))


# -- the helper, on constructed paths (no network) ----------------------------


class TestUncInsideAllowedRoot:
    def test_path_inside_the_unc_root_is_accepted(self, allowed):
        allowed(SHARE)
        assert _inside(SHARE + r"\P2026-001\10_strojni\risba.dwg")

    def test_the_root_itself_is_accepted(self, allowed):
        allowed(SHARE)
        assert _inside(SHARE)

    def test_comparison_ignores_case_like_windows(self, allowed):
        allowed(SHARE)
        assert _inside(SHARE.upper() + r"\RISBA.DWG")

    def test_sibling_folder_is_refused(self, allowed):
        allowed(SHARE)
        assert not _inside(r"\\vmware-host\Shared Folders\100_AGENTS-WORK\.claude\x.dxf")

    def test_name_prefix_is_not_containment(self, allowed):
        allowed(SHARE)
        assert not _inside(SHARE + r"2\x.dxf")

    def test_other_share_on_the_same_host_is_refused(self, allowed):
        allowed(SHARE)
        assert not _inside(r"\\vmware-host\Other\AGENT-TEST\x.dxf")

    def test_no_allowed_paths_means_no_unc(self, allowed):
        allowed()
        assert not _inside(SHARE + r"\x.dxf")

    def test_a_drive_letter_root_does_not_open_unc(self, allowed):
        allowed(r"C:\work")
        assert not _inside(SHARE + r"\x.dxf")

    @pytest.mark.parametrize(
        "root, path",
        [
            (r"\\localhost\C$", r"\\localhost\C$\Windows\win.ini"),
            (r"\\fileserver\D$", r"\\fileserver\D$\data\x.dxf"),
            (r"\\localhost\share", r"\\localhost\share\x.dxf"),
            (r"\\127.0.0.1\share", r"\\127.0.0.1\share\x.dxf"),
            (r"\\127.0.0.2\share", r"\\127.0.0.2\share\x.dxf"),
        ],
    )
    def test_admin_shares_and_the_local_host_stay_refused_even_if_configured(
        self, allowed, root, path
    ):
        allowed(root)
        assert not _inside(path)

    def test_own_computer_name_stays_refused_even_if_configured(self, allowed, monkeypatch):
        monkeypatch.setenv("COMPUTERNAME", "MECHANICAL-DEP")
        allowed(r"\\mechanical-dep\share")
        assert not _inside(r"\\MECHANICAL-DEP\share\x.dxf")

    @pytest.mark.parametrize(
        "root, path",
        [
            (r"\\?\C:\work", r"\\?\C:\work\x.dxf"),
            (r"\\.\C:\work", r"\\.\C:\work\x.dxf"),
            (
                r"\\?\UNC\vmware-host\Shared Folders\100_AGENTS-WORK",
                r"\\?\UNC\vmware-host\Shared Folders\100_AGENTS-WORK\x.dxf",
            ),
        ],
    )
    def test_extended_and_device_anchors_stay_refused_even_if_configured(
        self, allowed, root, path
    ):
        allowed(root)
        assert not _inside(path)


# -- validate_path: the v1 guards are unchanged --------------------------------


class TestGuardsUnchanged:
    def test_admin_share_is_still_rejected_with_unc_allowed(self, allowed):
        allowed(SHARE, r"\\localhost\C$")
        with pytest.raises(ToolError, match="not a plain local drive path"):
            validate_path("//localhost/C$/Windows/win.ini")

    def test_extended_length_is_still_rejected_with_unc_allowed(self, allowed):
        allowed(SHARE)
        with pytest.raises(ToolError, match="not a plain local drive path"):
            validate_path("//?/C:/Windows/win.ini")

    def test_device_form_still_reaches_the_system_directory_check(self, allowed):
        allowed(SHARE, r"C:\Windows")
        with pytest.raises(ToolError, match="system directory"):
            validate_path("//./C:/Windows/win.ini")

    def test_backslash_unc_input_is_still_a_pattern_rejection(self, allowed):
        allowed(SHARE)
        with pytest.raises(ToolError, match="traversal"):
            validate_path(SHARE + r"\x.dxf")

    def test_traversal_is_still_rejected(self, allowed):
        allowed(SHARE)
        with pytest.raises(ToolError, match="traversal"):
            validate_path(r"Z:\AGENT-TEST\..\CLAUDE.md")


# -- the real Z: drive (HEAD-DEP / MECHANICAL-DEP) ------------------------------


def _z_is_unc() -> bool:
    try:
        return Path("Z:/AGENT-TEST").is_dir() and Path("Z:/").resolve().drive.startswith("\\\\")
    except OSError:
        return False


NARROWED = r"Z:\AGENT-TEST," + SHARE


@pytest.mark.skipif(not _z_is_unc(), reason="needs Z: mapped to a UNC share")
class TestRealZDrive:
    def test_save_target_on_z_is_accepted(self, allowed_env):
        allowed_env(NARROWED)
        result = validate_path(r"Z:\AGENT-TEST\zn_unc_test.dxf", allow_write=True)
        assert result.drive.startswith("\\\\")
        assert result.parent.exists()

    def test_drive_entry_alone_is_enough(self, allowed_env):
        allowed_env(r"Z:\AGENT-TEST")
        assert validate_path(r"Z:\AGENT-TEST\zn_unc_test.dxf", allow_write=True)

    def test_unc_entry_alone_is_enough(self, allowed_env):
        allowed_env(SHARE)
        assert validate_path(r"Z:\AGENT-TEST\zn_unc_test.dxf", allow_write=True)

    def test_forward_slash_unc_inside_the_root_is_accepted(self, allowed_env):
        allowed_env(NARROWED)
        path = "//vmware-host/Shared Folders/100_AGENTS-WORK/AGENT-TEST/zn_unc_test.dxf"
        assert validate_path(path, allow_write=True)

    def test_z_outside_the_narrowed_root_is_rejected(self, allowed_env):
        allowed_env(NARROWED)
        with pytest.raises(ToolError, match="not a plain local drive path"):
            validate_path(r"Z:\CLAUDE.md")

    def test_z_is_rejected_when_only_a_local_root_is_allowed(self, allowed_env, tmp_path):
        allowed_env(str(tmp_path))
        with pytest.raises(ToolError, match="not a plain local drive path"):
            validate_path(r"Z:\AGENT-TEST\zn_unc_test.dxf")

    def test_z_is_rejected_without_allowed_paths(self, allowed_env):
        allowed_env("")
        with pytest.raises(ToolError, match="not a plain local drive path"):
            validate_path(r"Z:\AGENT-TEST\zn_unc_test.dxf")

    def test_write_still_requires_an_existing_parent(self, allowed_env):
        allowed_env(NARROWED)
        with pytest.raises(ToolError, match="Parent directory"):
            validate_path(r"Z:\AGENT-TEST\ne_obstaja_zn_unc\x.dxf", allow_write=True)
