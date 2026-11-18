"""Named organizer credentials and replay protection."""


def apply(db):
    db.execute("""CREATE TABLE IF NOT EXISTS named_admins (
        account_id TEXT PRIMARY KEY REFERENCES accounts(id),
        username TEXT NOT NULL COLLATE NOCASE UNIQUE,
        totp_seed TEXT NOT NULL,
        last_step INTEGER NOT NULL
    )""")
