"""MCP server exposing security-constrained work dispatch as typed tools.

This is an optional integration: install it with ``pip install dispatch-mcp[mcp]``.
The core package keeps its runtime dependencies minimal (stdlib only); the
``mcp`` SDK is required only to run this server.

Every tool is a thin wrapper over :class:`dispatch_mcp.core.Dispatcher`, so the
allowlist enforcement, fixed command template, and audit logging live in
exactly one place. Tools take structured inputs and return JSON objects.
Expected failures (unknown orguser/repo, empty task, dispatch error) surface as
``ToolError`` with a clean message.

Security boundary (deliberate omissions)
-----------------------------------------
* There is **no** ``run_shell`` / ``exec`` tool. The only thing a caller can do
  is *dispatch a unit of work* to an allowlisted lane; the command that runs is
  fixed by the server, never chosen by the caller.
* ``orguser`` and ``repo`` are resolved against a config (the ``.repos.json``
  shape). Unknown or mismatched targets are rejected before anything runs.
* The ``task_description`` is passed to the dispatch command as a single data
  argument (argv element), never interpolated into a shell string.
* No credential is embedded. The org-user's identity is resolved by the runtime
  when the dispatch runs as that user.

Configuration is resolved at call time from the environment:
``DISPATCH_REPOS_JSON`` (path to the allowlist; defaults to ``~/.repos.json``),
``DISPATCH_SSH_HOST``, ``DISPATCH_MAX_ITERATIONS``, and ``DISPATCH_TIMEOUT``.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError as error:  # pragma: no cover - import guard
    raise SystemExit(
        "dispatch-mcp server requires the 'mcp' package. Install it with: pip install 'dispatch-mcp[mcp]'"
    ) from error

from dispatch_mcp.core import DEFAULT_TIMEOUT, DispatchConfig, Dispatcher, DispatchError

INSTRUCTIONS = (
    "Security-constrained work dispatch. The only capability is dispatching a unit of "
    "work to an allowlisted org-user/repo lane; there is no arbitrary command execution. "
    "orguser and repo must resolve against the configured allowlist (the .repos.json "
    "shape) or the call is rejected. The task_description is passed to a fixed dispatch "
    "command as data, never as shell. Every dispatch is audited (who/what/when/handle). "
    "Use dispatch_unit to start work, dispatch_status/dispatch_result to poll a handle, "
    "and dispatch_list to see what is in flight. The dispatch methodology (tier choice, "
    "briefing, verification) lives in the dispatch-lane skill; this server is only the call."
)

# A single dispatcher per process. Config and runtime knobs are resolved once at
# build time from the environment; credentials are never read or stored here.
_DISPATCHER: Dispatcher | None = None


def _config_path() -> str:
    return os.environ.get("DISPATCH_REPOS_JSON", "~/.repos.json")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _build_dispatcher() -> Dispatcher:
    """Construct the dispatcher from environment config. Separated so tests can
    inject a fake transport and an explicit config instead."""
    config = DispatchConfig.from_file(_config_path())
    return Dispatcher(
        config,
        ssh_host=os.environ.get("DISPATCH_SSH_HOST", "localhost"),
        max_iterations=_int_env("DISPATCH_MAX_ITERATIONS", 50),
        timeout=_float_env("DISPATCH_TIMEOUT", DEFAULT_TIMEOUT),
    )


def set_dispatcher(dispatcher: Dispatcher | None) -> None:
    """Install the dispatcher the tools use (tests inject a fake-backed one)."""
    global _DISPATCHER
    _DISPATCHER = dispatcher


def _dispatcher() -> Dispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = _build_dispatcher()
    return _DISPATCHER


def _run(call: Any) -> dict[str, Any]:
    """Execute a dispatcher call, mapping expected failures to ``ToolError``."""
    try:
        return call()
    except DispatchError as error:
        raise ToolError(str(error)) from error


def dispatch_unit(orguser: str, repo: str, task_description: str) -> dict[str, Any]:
    """Dispatch a unit of work to an allowlisted org-user/repo lane.

    ``orguser`` and ``repo`` must resolve against the configured allowlist, and
    the repo must be owned by that org-user, or the call is rejected. The
    ``task_description`` is the brief for the lane; it is passed to a fixed
    dispatch command as data (never shell). Returns a handle to poll with
    ``dispatch_status`` / ``dispatch_result``.
    """
    dispatcher = _dispatcher()
    return _run(lambda: dispatcher.dispatch_unit(orguser, repo, task_description))


def dispatch_status(handle: str) -> dict[str, Any]:
    """Return the structured status of a previously dispatched handle."""
    dispatcher = _dispatcher()
    return _run(lambda: dispatcher.dispatch_status(handle))


def dispatch_result(handle: str) -> dict[str, Any]:
    """Return the structured result (exit code + detail) for a dispatched handle."""
    dispatcher = _dispatcher()
    return _run(lambda: dispatcher.dispatch_result(handle))


def dispatch_list() -> dict[str, Any]:
    """List every dispatch this server has launched this session."""
    dispatcher = _dispatcher()
    return _run(dispatcher.dispatch_list)


TOOLS = (
    dispatch_unit,
    dispatch_status,
    dispatch_result,
    dispatch_list,
)


def build_server() -> FastMCP:
    """Build the dispatch-mcp server with every dispatch tool registered."""
    server = FastMCP("dispatch-mcp", instructions=INSTRUCTIONS)
    for tool in TOOLS:
        server.add_tool(tool)
    return server


def main() -> None:
    """Run the dispatch-mcp server over stdio."""
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
