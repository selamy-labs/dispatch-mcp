"""Security-constrained dispatch core.

This module holds the dispatch logic exactly once. The MCP server in
:mod:`dispatch_mcp.mcp_server` is a thin wrapper that serialises these
structured results to JSON; nothing here imports the MCP SDK.

The capability exposed is narrow on purpose: *dispatch a unit of work to an
allowlisted org-user/repo lane*. The dispatch **methodology** (which tier, how
to brief, how to verify) stays a skill; this server is only the constrained
*call*.

Security model
--------------
* **Allowlist, not free-form targets.** ``orguser`` and ``repo`` must resolve
  against a :class:`DispatchConfig` loaded from the ``.repos.json`` shape.
  Unknown orguser or repo, or a repo not owned by the named orguser, is
  rejected before anything runs.
* **Fixed command template, never caller shell.** A dispatch runs one fixed
  argument vector (the templated dispatch command). The ``task_description`` is
  placed into that vector as a single element -- it is data, never interpolated
  into a shell string. There is no ``run_shell``/``exec`` tool and no code path
  that lets a caller choose the command.
* **No embedded credentials.** Nothing here stores a token or key. The
  org-user's identity (SSH key / ``GH_TOKEN``) is resolved by the runtime when
  the dispatch process runs as that user. Credentials never pass through this
  module.
* **Audit everything.** Every dispatch appends a structured record
  (who/what/when/handle/argv/outcome) to an injected :class:`AuditSink`.

All process execution goes through the injected :class:`Transport`, and all
timing through the injected :class:`Clock`, so the full validate/template/audit
path is exercised offline in tests with a fake transport -- no ssh, no tmux.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dispatch_mcp.transport import Clock, SubprocessTransport, SystemClock, Transport, TransportError

# A dispatched unit's lifecycle states.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

# Bounds on the free-text task description. It is passed as data (a single argv
# element), so metacharacters are harmless; the cap only stops abuse/runaway
# inputs, and the floor stops empty briefs.
MAX_TASK_LEN = 8_000
MIN_TASK_LEN = 1

# orguser / repo handles are restricted to a conservative identifier shape so a
# rejected lookup can never smuggle path traversal or metacharacters into the
# templated argv. (The allowlist is the real gate; this is defence in depth.)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

DEFAULT_TIMEOUT = 60.0


class DispatchError(Exception):
    """A dispatch request failed for an expected, user-facing reason.

    The MCP layer maps this to a ``ToolError`` so clients get a clean message
    instead of a stack trace.
    """


def _validate_handle(value: str, name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise DispatchError(f"{name} must not be empty")
    if not _HANDLE_RE.match(cleaned):
        raise DispatchError(
            f"invalid {name} {value!r}: must match {_HANDLE_RE.pattern} (letters, digits, dot, dash, underscore)"
        )
    return cleaned


def _validate_task(task_description: str) -> str:
    cleaned = task_description.strip()
    if len(cleaned) < MIN_TASK_LEN:
        raise DispatchError("task_description must not be empty")
    if len(cleaned) > MAX_TASK_LEN:
        raise DispatchError(f"task_description too long: {len(cleaned)} > {MAX_TASK_LEN} characters")
    return cleaned


@dataclass(frozen=True)
class RepoTarget:
    """One allowlisted dispatch target: a repo and the org-user that owns it."""

    repo: str
    orguser: str
    github: str


@dataclass(frozen=True)
class DispatchConfig:
    """The allowlist of valid dispatch targets, modelled on ``.repos.json``.

    Built from the ``.repos.json`` structure (``orgs`` + ``repos``). A target is
    valid only if the repo is listed and its declared ``org`` is a known
    org-user. There is no wildcard and no implicit target.
    """

    repos: dict[str, RepoTarget] = field(default_factory=dict)
    orgusers: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_repos_json(cls, raw: dict[str, Any]) -> DispatchConfig:
        """Build a config from a parsed ``.repos.json``-shaped mapping.

        Only repos whose ``org`` is a declared org-user become dispatch targets;
        a repo pointing at an unknown org is dropped (it cannot be dispatched to
        a user that does not exist).
        """
        orgs = raw.get("orgs", {})
        orgusers = frozenset(str(name) for name in orgs) if isinstance(orgs, dict) else frozenset()

        repos: dict[str, RepoTarget] = {}
        repos_raw = raw.get("repos", {})
        if isinstance(repos_raw, dict):
            for repo_name, entry in repos_raw.items():
                if not isinstance(entry, dict):
                    continue
                orguser = entry.get("org")
                github = entry.get("github", repo_name)
                if not isinstance(orguser, str) or orguser not in orgusers:
                    continue
                repos[str(repo_name)] = RepoTarget(repo=str(repo_name), orguser=orguser, github=str(github))
        return cls(repos=repos, orgusers=orgusers)

    @classmethod
    def from_file(cls, path: str | Path) -> DispatchConfig:
        """Load and parse a ``.repos.json`` file into a config."""
        text = Path(path).expanduser().read_text(encoding="utf-8")
        try:
            raw = json.loads(text)
        except ValueError as error:
            raise DispatchError(f"config {path} is not valid JSON: {error}") from error
        if not isinstance(raw, dict):
            raise DispatchError(f"config {path} must be a JSON object")
        return cls.from_repos_json(raw)

    def resolve(self, orguser: str, repo: str) -> RepoTarget:
        """Resolve ``(orguser, repo)`` against the allowlist or raise.

        Rejects an unknown repo, and rejects a known repo whose owning org-user
        does not match the requested ``orguser`` -- you may only dispatch a repo
        to the user that owns it.
        """
        orguser = _validate_handle(orguser, "orguser")
        repo = _validate_handle(repo, "repo")
        target = self.repos.get(repo)
        if target is None:
            raise DispatchError(
                f"unknown repo {repo!r}: not in the dispatch allowlist "
                f"({sorted(self.repos) or 'no targets configured'})"
            )
        if target.orguser != orguser:
            raise DispatchError(
                f"orguser {orguser!r} does not own repo {repo!r}; that repo dispatches to {target.orguser!r}"
            )
        return target


@dataclass(frozen=True)
class DispatchRecord:
    """An immutable audit record for one dispatch."""

    handle: str
    orguser: str
    repo: str
    github: str
    task_description: str
    argv: tuple[str, ...]
    requested_at: str
    status: str
    returncode: int | None = None
    detail: str = ""

    def to_audit(self) -> dict[str, Any]:
        """The structured who/what/when/handle record written to the audit log.

        The full ``task_description`` is summarised to a short preview so the
        audit stream stays scannable; the argv is recorded verbatim so it is
        always provable exactly what ran.
        """
        preview = self.task_description if len(self.task_description) <= 120 else self.task_description[:117] + "..."
        return {
            "event": "dispatch",
            "handle": self.handle,
            "orguser": self.orguser,
            "repo": self.repo,
            "github": self.github,
            "task_preview": preview,
            "argv": list(self.argv),
            "requested_at": self.requested_at,
            "status": self.status,
            "returncode": self.returncode,
            "detail": self.detail,
        }


class AuditSink(Protocol):
    """Receives one structured audit record per dispatch event."""

    def write(self, record: dict[str, Any]) -> None: ...


class ListAuditSink:
    """Default in-memory audit sink that retains every record it receives."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


def _new_handle() -> str:
    """A unique, unguessable dispatch handle (not a path or shell token)."""
    return f"dsp-{secrets.token_hex(8)}"


class Dispatcher:
    """Dispatches an allowlisted unit of work and tracks its handle.

    The only command this class ever runs is the fixed template returned by
    :meth:`_build_argv`. The ``task_description`` enters that command as a single
    argv element, so shell metacharacters in it are inert -- there is no shell.
    """

    def __init__(
        self,
        config: DispatchConfig,
        transport: Transport | None = None,
        *,
        clock: Clock | None = None,
        audit: AuditSink | None = None,
        ssh_host: str = "localhost",
        max_iterations: int = 50,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config
        self._transport = transport or SubprocessTransport()
        self._clock = clock or SystemClock()
        self._audit = audit or ListAuditSink()
        self._ssh_host = ssh_host
        self._max_iterations = max_iterations
        self._timeout = timeout
        # Handle -> latest record. In-memory by design: the durable record of a
        # dispatched lane lives in the lane's own state files; this registry is
        # the server's view of what it launched this session.
        self._records: dict[str, DispatchRecord] = {}

    def _build_argv(self, target: RepoTarget, task_description: str) -> list[str]:
        """Build the FIXED dispatch argv. The only caller-supplied value is the
        task_description, and it is a single list element -- never shell text.

        This mirrors the sanctioned ``dotfiles-dispatch <orguser> <repo>
        "<prompt>"`` lane shape, but as an argv with ``shell=False`` so nothing
        in it can be reinterpreted by a shell.
        """
        return [
            "dotfiles-dispatch",
            target.orguser,
            target.repo,
            task_description,
            "--host",
            self._ssh_host,
            "--max",
            str(self._max_iterations),
        ]

    def dispatch_unit(self, orguser: str, repo: str, task_description: str) -> dict[str, Any]:
        """Dispatch one unit of work to an allowlisted ``(orguser, repo)`` lane.

        Resolves the target against the allowlist (rejecting unknown/mismatched
        targets), runs the fixed dispatch template with the task as data, audits
        the event, and returns a structured handle the caller can poll.
        """
        task_description = _validate_task(task_description)
        target = self._config.resolve(orguser, repo)

        handle = _new_handle()
        requested_at = self._clock.now_iso()
        argv = self._build_argv(target, task_description)

        try:
            result = self._transport.run(argv, timeout=self._timeout)
        except TransportError as error:
            record = DispatchRecord(
                handle=handle,
                orguser=target.orguser,
                repo=target.repo,
                github=target.github,
                task_description=task_description,
                argv=tuple(argv),
                requested_at=requested_at,
                status=STATUS_FAILED,
                returncode=None,
                detail=f"transport failure: {error}",
            )
            self._remember(record)
            raise DispatchError(f"failed to launch dispatch for {target.github}: {error}") from error

        status = STATUS_RUNNING if result.ok else STATUS_FAILED
        detail = result.stdout.strip() if result.ok else (result.stderr.strip() or result.stdout.strip())
        record = DispatchRecord(
            handle=handle,
            orguser=target.orguser,
            repo=target.repo,
            github=target.github,
            task_description=task_description,
            argv=tuple(argv),
            requested_at=requested_at,
            status=status,
            returncode=result.returncode,
            detail=detail,
        )
        self._remember(record)

        if not result.ok:
            raise DispatchError(
                f"dispatch command for {target.github} exited {result.returncode}: "
                f"{detail or 'no output'} (handle {handle})"
            )
        return self._public_view(record)

    def dispatch_status(self, handle: str) -> dict[str, Any]:
        """Return the structured status of a previously dispatched handle."""
        record = self._lookup(handle)
        return self._public_view(record)

    def dispatch_result(self, handle: str) -> dict[str, Any]:
        """Return the structured result (exit code + detail) for a handle."""
        record = self._lookup(handle)
        view = self._public_view(record)
        view["returncode"] = record.returncode
        view["detail"] = record.detail
        return view

    def dispatch_list(self) -> dict[str, Any]:
        """List every dispatch this server has launched this session."""
        dispatches = [self._public_view(record) for record in self._records.values()]
        dispatches.sort(key=lambda item: item["requested_at"])
        return {"count": len(dispatches), "dispatches": dispatches}

    # -- internals -------------------------------------------------------------

    def _remember(self, record: DispatchRecord) -> None:
        self._records[record.handle] = record
        self._audit.write(record.to_audit())

    def _lookup(self, handle: str) -> DispatchRecord:
        cleaned = _validate_handle(handle, "handle")
        record = self._records.get(cleaned)
        if record is None:
            raise DispatchError(f"unknown dispatch handle {handle!r}")
        return record

    @staticmethod
    def _public_view(record: DispatchRecord) -> dict[str, Any]:
        """The structured, credential-free view returned to callers."""
        return {
            "handle": record.handle,
            "orguser": record.orguser,
            "repo": record.repo,
            "github": record.github,
            "status": record.status,
            "requested_at": record.requested_at,
        }
