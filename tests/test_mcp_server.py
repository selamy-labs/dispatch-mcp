"""Tests for the thin MCP wrapper, fully offline.

The server module is a thin adapter over :class:`dispatch_mcp.core.Dispatcher`:
it loads config, maps :class:`DispatchError` to ``ToolError``, and registers the
tools. Tests inject a fake-transport-backed dispatcher via ``set_dispatcher`` so
nothing spawns a process, and assert the wrapper's mapping and registration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from dispatch_mcp import mcp_server
from dispatch_mcp.core import Dispatcher
from tests.conftest import FakeClock, FakeTransport, RecordingAudit
from tests.fixtures import config, repos_json


@pytest.fixture(autouse=True)
def _reset_dispatcher() -> None:
    mcp_server.set_dispatcher(None)
    yield
    mcp_server.set_dispatcher(None)


def _install_fake() -> tuple[FakeTransport, RecordingAudit]:
    transport = FakeTransport()
    audit = RecordingAudit()
    mcp_server.set_dispatcher(Dispatcher(config(), transport, clock=FakeClock(), audit=audit))
    return transport, audit


def test_dispatch_unit_tool_round_trip() -> None:
    transport, audit = _install_fake()
    out = mcp_server.dispatch_unit("nash-forbes", "nash", "do the work")
    assert out["status"] == "running"
    assert out["github"] == "selamy-labs/nash"
    assert len(transport.calls) == 1
    assert len(audit.records) == 1

    status = mcp_server.dispatch_status(out["handle"])
    assert status["handle"] == out["handle"]
    result = mcp_server.dispatch_result(out["handle"])
    assert result["returncode"] == 0
    listing = mcp_server.dispatch_list()
    assert listing["count"] == 1


def test_unknown_target_maps_to_tool_error() -> None:
    _install_fake()
    with pytest.raises(ToolError, match="unknown repo"):
        mcp_server.dispatch_unit("nash-forbes", "ghost", "x")


def test_unknown_handle_maps_to_tool_error() -> None:
    _install_fake()
    with pytest.raises(ToolError, match="unknown dispatch handle"):
        mcp_server.dispatch_status("dsp-nope")


def test_build_server_registers_all_tools() -> None:
    _install_fake()
    server = mcp_server.build_server()
    assert server.name == "dispatch-mcp"
    # FastMCP holds a tool manager; assert each tool name is registered.
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert {"dispatch_unit", "dispatch_status", "dispatch_result", "dispatch_list"} <= names


def test_dispatcher_is_built_from_env_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No dispatcher pre-installed -> the server builds one from env config. We
    # point it at a temp .repos.json and a fake-friendly host; no process runs
    # because the resolve/reject path fires before any dispatch.
    path = tmp_path / ".repos.json"
    path.write_text(json.dumps(repos_json()), encoding="utf-8")
    monkeypatch.setenv("DISPATCH_REPOS_JSON", str(path))
    monkeypatch.setenv("DISPATCH_SSH_HOST", "localhost")
    monkeypatch.setenv("DISPATCH_MAX_ITERATIONS", "7")
    monkeypatch.setenv("DISPATCH_TIMEOUT", "12.5")
    mcp_server.set_dispatcher(None)

    # An unknown target is rejected by the env-built dispatcher without spawning.
    with pytest.raises(ToolError, match="unknown repo"):
        mcp_server.dispatch_unit("nash-forbes", "nope", "x")


def test_env_knobs_fall_back_on_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_MAX_ITERATIONS", "not-an-int")
    monkeypatch.setenv("DISPATCH_TIMEOUT", "not-a-float")
    assert mcp_server._int_env("DISPATCH_MAX_ITERATIONS", 50) == 50
    assert mcp_server._float_env("DISPATCH_TIMEOUT", 60.0) == 60.0


def test_env_knobs_read_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_MAX_ITERATIONS", "9")
    monkeypatch.setenv("DISPATCH_TIMEOUT", "3.5")
    assert mcp_server._int_env("DISPATCH_MAX_ITERATIONS", 50) == 9
    assert mcp_server._float_env("DISPATCH_TIMEOUT", 60.0) == 3.5


def test_env_knobs_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPATCH_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("DISPATCH_TIMEOUT", raising=False)
    assert mcp_server._int_env("DISPATCH_MAX_ITERATIONS", 50) == 50
    assert mcp_server._float_env("DISPATCH_TIMEOUT", 60.0) == 60.0
    assert mcp_server._config_path() == "~/.repos.json"


def test_main_runs_the_built_server(monkeypatch: pytest.MonkeyPatch) -> None:
    # main() builds the server and runs it over stdio; stub the blocking run.
    ran: list[bool] = []
    monkeypatch.setattr(FastMCP, "run", lambda self, *a, **k: ran.append(True))
    mcp_server.main()
    assert ran == [True]
