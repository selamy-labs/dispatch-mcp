"""Tests for the real subprocess transport, exercised against harmless binaries.

These never ssh anywhere and never touch the network -- they run trivial,
universally-available commands (``true``, ``false``, ``printf``) so the
production code path (:class:`SubprocessTransport`) gets real coverage offline.
The key assertion is that arguments are passed verbatim with ``shell=False``,
so a value containing shell metacharacters is never interpreted by a shell.
"""

from __future__ import annotations

import pytest

from dispatch_mcp.transport import Result, SubprocessTransport, SystemClock, TransportError


def test_subprocess_success() -> None:
    result = SubprocessTransport().run(["true"], timeout=5.0)
    assert isinstance(result, Result)
    assert result.ok
    assert result.returncode == 0
    assert result.argv == ("true",)


def test_subprocess_nonzero_exit_is_returned_not_raised() -> None:
    result = SubprocessTransport().run(["false"], timeout=5.0)
    assert not result.ok
    assert result.returncode != 0


def test_arguments_are_passed_verbatim_not_through_a_shell() -> None:
    # If this went through a shell, the metacharacters would be expanded or the
    # echo would be split; printf with shell=False prints the literal string.
    payload = "$(echo pwned); rm -rf / && `whoami`"
    result = SubprocessTransport().run(["printf", "%s", payload], timeout=5.0)
    assert result.ok
    assert result.stdout == payload  # exact, unexpanded


def test_missing_command_raises_transport_error() -> None:
    with pytest.raises(TransportError, match="not found"):
        SubprocessTransport().run(["dispatch-mcp-no-such-binary-xyz"], timeout=5.0)


def test_timeout_raises_transport_error() -> None:
    with pytest.raises(TransportError, match="timed out"):
        SubprocessTransport().run(["sleep", "5"], timeout=0.05)


def test_os_error_on_launch_raises_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A spawn-time OSError (not FileNotFound/Timeout) also surfaces cleanly.
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("exec format error")

    import dispatch_mcp.transport as transport_mod

    monkeypatch.setattr(transport_mod.subprocess, "run", _boom)
    with pytest.raises(TransportError, match="failed to launch"):
        SubprocessTransport().run(["whatever"], timeout=5.0)


def test_system_clock_now_iso_and_monotonic() -> None:
    clock = SystemClock()
    stamp = clock.now_iso()
    assert stamp.endswith("Z")
    assert clock.monotonic_ns() >= 0
