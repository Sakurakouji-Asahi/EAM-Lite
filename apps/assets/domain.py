"""Shared asset lifecycle meanings, independent of persistence and permissions."""

MANAGED_ASSET_STATUSES = (
    "pending_label", "in_use", "idle", "loaned", "under_repair", "pending_disposal",
)
TERMINAL_ASSET_STATUSES = frozenset({"disposed", "sold", "other_disposed"})
