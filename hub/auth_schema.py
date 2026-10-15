"""Additive authentication receipts; no credential IDs or grants are replaced."""


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS oauth_refresh_receipts (
        predecessor_hash TEXT PRIMARY KEY REFERENCES tokens(hash) ON DELETE CASCADE,
        grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
        successor_hash TEXT NOT NULL,
        response TEXT NOT NULL,
        expires REAL NOT NULL
    )""")
    db.execute('CREATE INDEX IF NOT EXISTS oauth_refresh_receipts_expiry ON oauth_refresh_receipts(expires)')
