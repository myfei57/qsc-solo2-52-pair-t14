"""Record kinds used by the audit side."""

from __future__ import annotations

ENTRY_KIND = "audit.entry"
ROLLUP_KIND = "audit.rollup"
ROLLBACK_STARTED_KIND = "audit.rollback_started"
ROLLBACK_REALIGNED_KIND = "audit.rollback_realigned"
ROLLBACK_BLOCKED_KIND = "audit.rollback_blocked"
