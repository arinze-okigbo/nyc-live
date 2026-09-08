"""`density_now` / `density_history` for the MCP tools.

These are re-exported, not reimplemented. The served implementation lives in
`nyc_live.services.density` (owned by mcp-architect) and reads the
`density_samples` and `cameras` tables that `nyc_vision.pipeline` writes. There is
exactly one aggregation path so the MCP tool, the dashboard and this package can
never disagree about what "density" means.

The reader side needs nothing from nyc-vision at runtime: `nyc-mcp` imports
`nyc_live.services.density` directly. This module exists so that anything holding
onto the Phase 3 name gets the same functions.
"""

from __future__ import annotations

from nyc_live.services.density import (
    DEFAULT_BUCKET,
    DEFAULT_HISTORY_WINDOW,
    DEFAULT_NOW_WINDOW,
    density_history,
    density_now,
)

__all__ = [
    "DEFAULT_BUCKET",
    "DEFAULT_HISTORY_WINDOW",
    "DEFAULT_NOW_WINDOW",
    "density_history",
    "density_now",
]
