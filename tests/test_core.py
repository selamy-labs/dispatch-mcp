"""Tests for the security-constrained dispatch core, fully offline.

No process is spawned and no ssh/tmux is touched: a :class:`FakeTransport`
stands in for the real subprocess, a :class:`FakeClock` makes handles and
timestamps deterministic, and a :class:`RecordingAudit` captures the audit
stream. The focus is the security contract -- allowlist enforcement, no shell
injection, and audit -- plus the dispatch -> status -> result -> list lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatch_mcp.core import (
    STATUS_FAILED,
    STATUS_RUNNING,
    DispatchConfig,
    Dispatcher,
    DispatchError,
    ListAuditSink,
)
from tests.conftest import FakeClock, FakeTransport, RecordingAudit
from tests.fixtures import config, repos_json


def _dispatcher(
    transport: FakeTransport | None = None,
    audit: RecordingAudit | None = None,
) -> tuple[Dispatcher, FakeTransport, RecordingAudit]:
    transport = transport or FakeTransport()
    audit = audit or RecordingAudit()
    dispatcher = Dispatcher(config(), transport, clock=FakeClock(), audit=audit, ssh_host="localhost")
    return dispatcher, transport, audit


# -- Allowlist enforcement -----------------------------------------------------


def test_dispatch_to_allowlisted_target_succeeds() -> None:
    dispatcher, transport, _ = _dispatcher()
    result = dispatcher.dispatch_unit("nash-forbes", "nash", "fix the verifier race")
    assert result["status"] == STATUS_RUNNING
    assert result["github"] == "selamy-labs/nash"
    assert result["handle"].startswith("dsp-")
    assert len(transport.calls) == 1


def test_unknown_repo_is_rejected_before_running() -> None:
    dispatcher, transport, _ = _dispatcher()
    with pytest.raises(DispatchError, match="unknown repo"):
        dispatcher.dispatch_unit("nash-forbes", "does-not-exist", "do a thing")
    assert transport.calls == []  # nothing ran


def test_unknown_orguser_is_rejected_before_running() -> None:
    dispatcher, transport, _ = _dispatcher()
    # 'reid' is real but owned by reid-max, not nash-forbes.
    with pytest.raises(DispatchError, match="does not own repo"):
        dispatcher.dispatch_unit("nash-forbes", "reid", "do a thing")
    assert transport.calls == []


def test_handle_shape_is_enforced_on_orguser() -> None:
    dispatcher, transport, _ = _dispatcher()
    with pytest.raises(DispatchError, match="invalid orguser"):
        dispatcher.dispatch_unit("nash;rm -rf /", "nash", "do a thing")
    assert transport.calls == []


def test_empty_orguser_and_repo_rejected() -> None:
    dispatcher, _, _ = _dispatcher()
    with pytest.raises(DispatchError, match="orguser must not be empty"):
        dispatcher.dispatch_unit("   ", "nash", "task")
    with pytest.raises(DispatchError, match="repo must not be empty"):
        dispatcher.dispatch_unit("nash-forbes", "  ", "task")


# -- No shell injection --------------------------------------------------------


def test_task_with_shell_metachars_is_passed_as_data_not_executed() -> None:
    dispatcher, transport, _ = _dispatcher()
    payload = 'oops"; rm -rf / #  $(curl evil) `whoami` && echo pwned'
    dispatcher.dispatch_unit("reid-max", "reid", payload)

    argv = transport.calls[0]["argv"]
    # The dangerous text is exactly one argv element, verbatim -- never split,
    # never concatenated into a shell string.
    assert payload in argv
    assert argv.count(payload) == 1
    # The fixed template is what actually runs; the caller never chose it.
    assert argv[0] == "dotfiles-dispatch"
    assert argv[1] == "reid-max"
    assert argv[2] == "reid"
    assert argv[3] == payload
    # No element is a shell invocation.
    assert not any(token in ("sh", "bash", "-c", "/bin/sh") for token in argv)


def test_empty_task_description_rejected() -> None:
    dispatcher, transport, _ = _dispatcher()
    with pytest.raises(DispatchError, match="task_description must not be empty"):
        dispatcher.dispatch_unit("nash-forbes", "nash", "   ")
    assert transport.calls == []


def test_too_long_task_description_rejected() -> None:
    dispatcher, _, _ = _dispatcher()
    with pytest.raises(DispatchError, match="too long"):
        dispatcher.dispatch_unit("nash-forbes", "nash", "x" * 9000)


# -- Lifecycle: dispatch -> status -> result -> list ---------------------------


def test_dispatch_status_result_round_trip() -> None:
    dispatcher, _, _ = _dispatcher()
    dispatched = dispatcher.dispatch_unit("nash-forbes", "nash", "ship it")
    handle = dispatched["handle"]

    status = dispatcher.dispatch_status(handle)
    assert status["handle"] == handle
    assert status["status"] == STATUS_RUNNING
    assert "returncode" not in status  # status view stays minimal

    result = dispatcher.dispatch_result(handle)
    assert result["handle"] == handle
    assert result["returncode"] == 0
    assert "detail" in result


def test_dispatch_list_reports_in_flight() -> None:
    dispatcher, _, _ = _dispatcher()
    a = dispatcher.dispatch_unit("nash-forbes", "nash", "task a")
    b = dispatcher.dispatch_unit("reid-max", "reid", "task b")

    listing = dispatcher.dispatch_list()
    assert listing["count"] == 2
    handles = {item["handle"] for item in listing["dispatches"]}
    assert handles == {a["handle"], b["handle"]}
    # Sorted by requested_at (deterministic fake clock).
    times = [item["requested_at"] for item in listing["dispatches"]]
    assert times == sorted(times)


def test_unknown_handle_rejected_for_status_and_result() -> None:
    dispatcher, _, _ = _dispatcher()
    with pytest.raises(DispatchError, match="unknown dispatch handle"):
        dispatcher.dispatch_status("dsp-deadbeef")
    with pytest.raises(DispatchError, match="unknown dispatch handle"):
        dispatcher.dispatch_result("dsp-deadbeef")


def test_empty_list_when_nothing_dispatched() -> None:
    dispatcher, _, _ = _dispatcher()
    listing = dispatcher.dispatch_list()
    assert listing == {"count": 0, "dispatches": []}


# -- Failure handling ----------------------------------------------------------


def test_nonzero_exit_raises_and_records_failure() -> None:
    transport = FakeTransport()
    transport.queue_result(returncode=2, stderr="dispatch refused: stale token")
    dispatcher, _, audit = _dispatcher(transport)

    with pytest.raises(DispatchError, match="exited 2"):
        dispatcher.dispatch_unit("nash-forbes", "nash", "task")

    # Even a failed dispatch is audited and queryable by its handle.
    assert len(audit.records) == 1
    assert audit.records[0]["status"] == STATUS_FAILED
    listing = dispatcher.dispatch_list()
    assert listing["count"] == 1
    assert listing["dispatches"][0]["status"] == STATUS_FAILED


def test_transport_failure_raises_and_records_failure() -> None:
    transport = FakeTransport()
    transport.queue_error("ssh: connect timeout")
    dispatcher, _, audit = _dispatcher(transport)

    with pytest.raises(DispatchError, match="failed to launch dispatch"):
        dispatcher.dispatch_unit("reid-max", "reid", "task")

    assert len(audit.records) == 1
    assert audit.records[0]["status"] == STATUS_FAILED
    assert audit.records[0]["returncode"] is None


# -- Audit ---------------------------------------------------------------------


def test_every_dispatch_emits_a_structured_audit_record() -> None:
    dispatcher, _, audit = _dispatcher()
    dispatcher.dispatch_unit("nash-forbes", "nash", "do the thing carefully")

    assert len(audit.records) == 1
    record = audit.records[0]
    assert record["event"] == "dispatch"
    assert record["orguser"] == "nash-forbes"
    assert record["repo"] == "nash"
    assert record["github"] == "selamy-labs/nash"
    assert record["status"] == STATUS_RUNNING
    assert record["requested_at"].startswith("2026-06-17T")
    # The argv is recorded verbatim so it is always provable what ran.
    assert record["argv"][0] == "dotfiles-dispatch"
    assert "do the thing carefully" in record["argv"]


def test_audit_task_preview_is_truncated() -> None:
    dispatcher, _, audit = _dispatcher()
    long_task = "y" * 300
    dispatcher.dispatch_unit("nash-forbes", "nash", long_task)
    preview = audit.records[0]["task_preview"]
    assert len(preview) <= 120
    assert preview.endswith("...")


def test_default_audit_sink_retains_records() -> None:
    # Exercise the production default ListAuditSink (no audit injected).
    sink = ListAuditSink()
    dispatcher = Dispatcher(config(), FakeTransport(), clock=FakeClock(), audit=sink)
    dispatcher.dispatch_unit("nash-forbes", "nash", "task")
    assert len(sink.records) == 1


# -- Config loading ------------------------------------------------------------


def test_config_drops_repo_with_unknown_org_and_malformed_entry() -> None:
    cfg = config()
    # orphan -> unknown org, malformed -> not a dict: both excluded.
    assert set(cfg.repos) == {"nash", "reid"}
    assert "orphan" not in cfg.repos
    assert "malformed" not in cfg.repos


def test_config_from_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".repos.json"
    path.write_text(json.dumps(repos_json()), encoding="utf-8")
    cfg = DispatchConfig.from_file(path)
    target = cfg.resolve("nash-forbes", "nash")
    assert target.github == "selamy-labs/nash"


def test_config_from_file_rejects_bad_json(tmp_path: Path) -> None:
    path = tmp_path / ".repos.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DispatchError, match="not valid JSON"):
        DispatchConfig.from_file(path)


def test_config_from_file_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / ".repos.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(DispatchError, match="must be a JSON object"):
        DispatchConfig.from_file(path)


def test_config_from_non_dict_orgs_and_repos_is_empty() -> None:
    cfg = DispatchConfig.from_repos_json({"orgs": [], "repos": []})
    assert cfg.repos == {}
    assert cfg.orgusers == frozenset()


def test_resolve_with_no_targets_configured_message() -> None:
    cfg = DispatchConfig.from_repos_json({"orgs": {}, "repos": {}})
    with pytest.raises(DispatchError, match="no targets configured"):
        cfg.resolve("nash-forbes", "nash")
