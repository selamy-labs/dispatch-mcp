"""Shared offline test doubles: a fake transport, clock, and audit sink.

Nothing in the test suite spawns a process or touches ssh/tmux. The fake
transport records every argv it was asked to run and returns queued results (or
raises a queued error). The fake clock yields deterministic timestamps and a
monotonic counter so handles and audit records are stable. The recording audit
sink lets tests assert that every dispatch was logged.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from dispatch_mcp.transport import Result, TransportError


class FakeTransport:
    """Returns queued results in order; records every argv it received.

    Defaults to a clean success (exit 0) when nothing is queued, so the common
    happy-path test does not have to pre-load a result. Queue an explicit
    failure or error to exercise the non-zero / transport-failure paths.
    """

    def __init__(self) -> None:
        self._queue: deque[Result | Exception] = deque()
        self.calls: list[dict[str, Any]] = []

    def queue_result(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self._queue.append(Result(returncode=returncode, stdout=stdout, stderr=stderr))

    def queue_error(self, message: str = "boom") -> None:
        self._queue.append(TransportError(message))

    def run(self, argv: list[str], *, timeout: float) -> Result:
        self.calls.append({"argv": list(argv), "timeout": timeout})
        if not self._queue:
            return Result(returncode=0, stdout="dispatched", stderr="", argv=tuple(argv))
        item = self._queue.popleft()
        if isinstance(item, Exception):
            raise item
        # Attach the real argv so assertions can inspect exactly what ran.
        return Result(returncode=item.returncode, stdout=item.stdout, stderr=item.stderr, argv=tuple(argv))


class FakeClock:
    """A deterministic clock: fixed-format ISO time and a counting monotonic."""

    def __init__(self) -> None:
        self._seq = 0

    def now_iso(self) -> str:
        self._seq += 1
        return f"2026-06-17T00:00:{self._seq:02d}Z"

    def monotonic_ns(self) -> int:
        self._seq += 1
        return self._seq


class RecordingAudit:
    """An audit sink that retains every record for assertions."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)
