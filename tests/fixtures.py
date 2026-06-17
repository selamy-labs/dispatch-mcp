"""Canned ``.repos.json``-shaped config used to drive the dispatcher offline.

This is a minimal, sanitized allowlist shaped like the real ``.repos.json``
(``orgs`` + ``repos``). It is hand-built for tests, not copied from any live
config, and contains no credentials.
"""

from __future__ import annotations

from typing import Any

from dispatch_mcp.core import DispatchConfig


def repos_json() -> dict[str, Any]:
    """A small allowlist: two valid targets plus one repo with a bad org.

    ``orphan`` points at an org-user that is not declared, so it must be dropped
    from the allowlist (cannot dispatch to a user that does not exist).
    """
    return {
        "orgs": {
            "nash-forbes": {"git_user": "Nash Forbes", "expected_github_actor": "nash-forbes"},
            "reid-max": {"git_user": "Reid Max", "expected_github_actor": "reid-max"},
        },
        "repos": {
            "nash": {"org": "nash-forbes", "github": "selamy-labs/nash"},
            "reid": {"org": "reid-max", "github": "selamy-labs/reid"},
            "orphan": {"org": "ghost-user", "github": "selamy-labs/orphan"},
            "malformed": "not-a-dict",
        },
    }


def config() -> DispatchConfig:
    """The parsed allowlist most tests dispatch against."""
    return DispatchConfig.from_repos_json(repos_json())
