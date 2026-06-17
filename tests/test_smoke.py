"""Smoke test for the scaffold: the package imports and exposes a version."""

from __future__ import annotations

import dispatch_mcp


def test_version_is_exposed() -> None:
    assert dispatch_mcp.__version__
