"""After AutoCAD restarts, the next call attaches again (fork, popravek/com-ponovna-povezava).

Live 2026-11-24: AutoCAD was closed and started again; every MCP call then
answered 0x800706BA "The RPC server is unavailable" until the MCP server was
restarted, because the dead Application proxy stayed cached.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("hr", [-2147023174, -2147023170, -2147417848])
async def test_a_dead_server_drops_the_cached_proxy(hr, monkeypatch):
    pywintypes = pytest.importorskip("pywintypes")
    import backends.com_backend as cb

    backend = cb.ComBackend()
    backend._executor = backend._new_executor()
    monkeypatch.setitem(cb._COM_STATE, "app", object())
    backend._connected = True

    def _dead():
        raise pywintypes.com_error(hr, "The RPC server is unavailable.", None, None)

    try:
        with pytest.raises(RuntimeError, match="AutoCAD COM error"):
            await backend._run(_dead)
    finally:
        backend._executor.shutdown(wait=False)
    assert "app" not in cb._COM_STATE, "the next call runs GetActiveObject again"
    assert backend._connected is False


async def test_an_ordinary_com_error_keeps_the_proxy(monkeypatch):
    pywintypes = pytest.importorskip("pywintypes")
    import backends.com_backend as cb

    backend = cb.ComBackend()
    backend._executor = backend._new_executor()
    proxy = object()
    monkeypatch.setitem(cb._COM_STATE, "app", proxy)

    def _refused():
        raise pywintypes.com_error(-2147352567, "Exception occurred.", None, None)

    try:
        with pytest.raises(RuntimeError):
            await backend._run(_refused)
    finally:
        backend._executor.shutdown(wait=False)
    assert cb._COM_STATE.get("app") is proxy
