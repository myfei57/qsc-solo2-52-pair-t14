"""Stage rollback coordinator.

Binds together the three things that must agree about a rollback:

* the stage machines (which phase a line sits at and its append-only steps),
* the rollback guard / pre gate (what is blocked while a window is open),
* the audit ledger and the committed record stream (the durable history).

The coordinator is the only place allowed to open or confirm a window. The
target stage is never a caller choice: it is fixed to the previous stable
stage of the line.
"""

from __future__ import annotations

from .coordinator import (
    ROLLBACK_BLOCK_KIND,
    ROLLBACK_CONFIRM_KIND,
    ROLLBACK_KIND,
    RollbackCoordinator,
)

__all__ = [
    "ROLLBACK_BLOCK_KIND",
    "ROLLBACK_CONFIRM_KIND",
    "ROLLBACK_KIND",
    "RollbackCoordinator",
]
