"""Additive plugin state. No old role, grant, account or receipt gains permission."""

SCHEMA = '''
CREATE TABLE IF NOT EXISTS external_integrations (
    id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
    owner_user_id TEXT NOT NULL REFERENCES users(id), alias TEXT NOT NULL,
    plugin_id TEXT NOT NULL, manifest_hash TEXT NOT NULL, manifest TEXT NOT NULL,
    backends TEXT NOT NULL, config TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, UNIQUE(space_id,alias));
CREATE TABLE IF NOT EXISTS project_integrations (
    id TEXT PRIMARY KEY, integration_id TEXT NOT NULL REFERENCES external_integrations(id),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1, UNIQUE(integration_id,project_id));
CREATE TABLE IF NOT EXISTS external_plugin_consents (
    grant_id TEXT PRIMARY KEY REFERENCES grants(id), space_id TEXT NOT NULL,
    user_id TEXT NOT NULL, profile_id TEXT NOT NULL, role_id TEXT NOT NULL,
    snapshot TEXT NOT NULL, created REAL NOT NULL);
'''


def migrate(db):
    row = db.execute("SELECT value FROM meta WHERE key='external_plugin_schema'").fetchone()
    if row and row[0] != '1':
        raise RuntimeError('Unsupported external plugin database schema')
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    if 'plugin_context' not in {row[1] for row in db.execute('PRAGMA table_info(gateway_calls)')}:
        db.execute('ALTER TABLE gateway_calls ADD COLUMN plugin_context TEXT')
    db.execute("INSERT OR IGNORE INTO meta VALUES ('external_plugin_schema','1')")
