"""Move optional content out of the backed-up event ledger and retire its old capture default."""
from ..gateway.accounting import now_ms
from ..gateway.content_store import reset_legacy_capture


def apply(db, event):
    reset_legacy_capture(db)
    event.keys.ledger.content.migrate(db, now_ms())
