"""Smoke test for the package scaffold.

The implementation (core, backend, MCP wrapper, and full offline suite) lands in
the follow-up PR; this keeps the scaffold importable and CI meaningful.
"""

from __future__ import annotations

import telemetry_mcp


def test_version_is_exposed() -> None:
    assert telemetry_mcp.__version__ == "0.1.0"
