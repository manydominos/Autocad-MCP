"""Tests for the local v3 ``ALLOW_NO_AUTH`` opt-out of the HTTP bind guard.

Without ``ALLOW_NO_AUTH`` a non-loopback HTTP bind without ``MCP_AUTH_TOKEN``
is refused exactly as before. With ``ALLOW_NO_AUTH=true`` (and
``ALLOW_REMOTE_HTTP=true``) the bind is allowed and a clear warning is logged.
Loopback and token behaviour are unchanged.
"""

from __future__ import annotations

import logging
import sys

import pytest

REMOTE = "172.30.50.14"


def _server(monkeypatch, *, allow_remote, token, allow_no_auth, allowed_paths=("C:/x",)):
    sys.modules.pop("server", None)
    import config as cfg
    import server as srv

    monkeypatch.setattr(cfg.settings, "allow_remote_http", allow_remote, raising=False)
    monkeypatch.setattr(cfg.settings, "mcp_auth_token", token, raising=False)
    monkeypatch.setattr(cfg.settings, "allow_no_auth", allow_no_auth, raising=False)
    monkeypatch.setattr(cfg.settings, "allowed_paths", list(allowed_paths), raising=False)
    return srv


# -- config ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("false", False), ("", False), ("1", False), ("yes", False),
     ("true", True), ("TRUE", True), (" True ", True)],
)
def test_allow_no_auth_is_read_from_the_environment(monkeypatch, value, expected):
    import config as cfg

    if value is None:
        monkeypatch.delenv("ALLOW_NO_AUTH", raising=False)
    else:
        monkeypatch.setenv("ALLOW_NO_AUTH", value)
    assert cfg.Settings().allow_no_auth is expected


# -- without the setting: refused as before ------------------------------------


def test_without_setting_remote_without_token_is_refused(monkeypatch):
    srv = _server(monkeypatch, allow_remote=True, token="", allow_no_auth=False)
    with pytest.raises(SystemExit, match="without MCP_AUTH_TOKEN"):
        srv._validate_http_bind(REMOTE)


def test_without_setting_remote_without_flag_is_refused(monkeypatch):
    srv = _server(monkeypatch, allow_remote=False, token="", allow_no_auth=False)
    with pytest.raises(SystemExit, match="Refusing to bind HTTP on non-loopback"):
        srv._validate_http_bind(REMOTE)


# -- with the setting: start allowed, warning logged ---------------------------


def test_with_setting_remote_without_token_starts_and_warns(monkeypatch, caplog):
    srv = _server(monkeypatch, allow_remote=True, token="", allow_no_auth=True)
    with caplog.at_level(logging.WARNING, logger="server"):
        srv._validate_http_bind(REMOTE)  # must not raise
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("ALLOW_NO_AUTH" in m and "WITHOUT authentication" in m and REMOTE in m for m in msgs)
    assert srv._auth is None


def test_setting_does_not_replace_allow_remote_http(monkeypatch):
    """ALLOW_NO_AUTH only drops the token requirement, not the remote opt-in."""
    srv = _server(monkeypatch, allow_remote=False, token="", allow_no_auth=True)
    with pytest.raises(SystemExit, match="Refusing to bind HTTP on non-loopback"):
        srv._validate_http_bind(REMOTE)


def test_with_setting_and_token_the_token_path_is_unchanged(monkeypatch, caplog):
    srv = _server(monkeypatch, allow_remote=True, token="secret", allow_no_auth=True)
    with caplog.at_level(logging.WARNING, logger="server"):
        srv._validate_http_bind(REMOTE)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("Auth token required" in m for m in msgs)
    assert not any("ALLOW_NO_AUTH" in m for m in msgs)


def test_with_setting_loopback_is_silent(monkeypatch, caplog):
    srv = _server(monkeypatch, allow_remote=False, token="", allow_no_auth=True)
    with caplog.at_level(logging.WARNING, logger="server"):
        srv._validate_http_bind("127.0.0.1")
    assert not any("ALLOW_NO_AUTH" in r.getMessage() for r in caplog.records)


def test_only_a_real_true_counts(monkeypatch):
    """A truthy non-bool (e.g. a mocked settings attribute) must not open the guard."""
    srv = _server(monkeypatch, allow_remote=True, token="", allow_no_auth="true")
    with pytest.raises(SystemExit, match="without MCP_AUTH_TOKEN"):
        srv._validate_http_bind(REMOTE)


# -- the run_async guard path (fastmcp run ...) -------------------------------


def _probe(srv, monkeypatch):
    async def _started(*args, **kwargs):
        raise AssertionError("the server started")

    monkeypatch.setattr(srv, "_orig_run_async", _started, raising=False)


@pytest.mark.asyncio
async def test_run_async_without_setting_is_refused(monkeypatch):
    srv = _server(monkeypatch, allow_remote=True, token="", allow_no_auth=False)
    _probe(srv, monkeypatch)
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN"):
        await srv.mcp.run_async(transport="http", host=REMOTE)


@pytest.mark.asyncio
async def test_run_async_with_setting_starts(monkeypatch):
    srv = _server(monkeypatch, allow_remote=True, token="", allow_no_auth=True)
    _probe(srv, monkeypatch)
    with pytest.raises(AssertionError, match="the server started"):
        await srv.mcp.run_async(transport="http", host=REMOTE)


def test_with_setting_allowed_paths_is_still_required(monkeypatch):
    """1.8-17: a network bind without a token must still confine its files."""
    srv = _server(monkeypatch, allow_remote=True, token="", allow_no_auth=True, allowed_paths=())
    with pytest.raises(SystemExit, match="without ALLOWED_PATHS"):
        srv._validate_http_bind(REMOTE)
