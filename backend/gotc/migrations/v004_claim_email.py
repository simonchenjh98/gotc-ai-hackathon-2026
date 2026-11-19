"""Private team-application contact details and durable notification state."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS claim_contacts (
        request_id TEXT PRIMARY KEY REFERENCES claim_requests(id) ON DELETE CASCADE,
        email TEXT NOT NULL, normalized_email TEXT NOT NULL, created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS claim_contacts_email ON claim_contacts(normalized_email)",
    """CREATE TABLE IF NOT EXISTS claim_email_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES claim_requests(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('received','approved','rejected')),
        status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','suppressed','expired')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL, sent_at INTEGER, error TEXT NOT NULL DEFAULT '',
        UNIQUE(request_id,kind)
    )""",
    "CREATE INDEX IF NOT EXISTS claim_email_due ON claim_email_outbox(status,next_attempt_at)",
)


def apply(db):
    for statement in STATEMENTS:
        db.execute(statement)
