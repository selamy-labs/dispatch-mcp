"""Dispatch transport abstraction so the core stays fully offline-testable.

The dispatch core never shells out directly. It depends only on the
:class:`Transport` protocol below, which executes a **fixed argument vector**
(never a caller-supplied shell string) and returns a :class:`Result`.

Production code injects :class:`SubprocessTransport`, which runs the argv with
``subprocess.run`` and ``shell=False`` -- the one place a real process is
spawned. Tests inject a fake transport that records the argv and returns canned
results, so the full plan/validate/audit path is exercised offline with no ssh,
no tmux, and no network.

Two security properties live here and nowhere else:

* **No shell.** Every command is an ``argv`` list run with ``shell=False``. A
  ``task_description`` carrying shell metacharacters is just one element of that
  list -- it is data, never a token the shell can interpret. There is no code
  path that concatenates caller input into a shell string.
* **No embedded credentials.** The transport carries no tokens. Identity
  (the org-user's SSH key / ``GH_TOKEN``) is resolved by the runtime when the
  process runs as that user; it is never passed through this module.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Result:
    """The outcome of running one dispatch argv: exit code, stdout, stderr.

    ``argv`` is the exact argument vector that was executed, retained so the
    audit log can record precisely what ran (data, never a shell string).
    """

    returncode: int
    stdout: str
    stderr: str
    argv: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True when the command exited cleanly (exit code 0)."""
        return self.returncode == 0


class TransportError(Exception):
    """A transport failed to launch or complete a command (spawn, timeout)."""


class Transport(Protocol):
    """Runs a fixed argument vector and returns a :class:`Result`.

    Implementations must execute ``argv`` with ``shell=False`` -- the list is
    passed straight to the OS, so no element is ever interpreted by a shell.
    A non-zero exit is returned as a :class:`Result`, not raised, so the core
    decides how to surface it. Only spawn/timeout failures raise
    :class:`TransportError`.
    """

    def run(self, argv: list[str], *, timeout: float) -> Result: ...


class SubprocessTransport:
    """Production transport: runs the argv as a real subprocess.

    Uses ``shell=False`` (the default for a list argv) so the
    ``task_description`` and every other element are passed verbatim to the OS,
    never parsed by a shell. Carries no credentials of its own.
    """

    def run(self, argv: list[str], *, timeout: float) -> Result:
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, shell=False, no caller shell string
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except FileNotFoundError as error:
            raise TransportError(f"dispatch command not found: {argv[0]!r}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise TransportError(f"dispatch timed out after {timeout}s: {error}") from error
        except OSError as error:
            raise TransportError(f"dispatch failed to launch {argv[0]!r}: {error}") from error
        return Result(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            argv=tuple(argv),
        )


class Clock(Protocol):
    """A wall clock, injected so handles and audit timestamps are testable."""

    def now_iso(self) -> str: ...

    def monotonic_ns(self) -> int: ...


class SystemClock:
    """The real clock: UTC ISO timestamps and a monotonic nanosecond counter."""

    def now_iso(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()
