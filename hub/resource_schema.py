"""Additive resource/conversation migration, independent of the IAM schema.

No existing policy, encrypted credential, receipt or workflow is rewritten.
An old project/VPS association is not converted into permission.
"""


def migrate(db):
    for table, name, definition in (
        ('vps_connections', 'execution_project_id', 'TEXT REFERENCES projects(id) ON DELETE SET NULL'),
        ('grants', 'resource_policy', 'TEXT'),
        ('grants', 'connector_ceiling', 'TEXT'),
    ):
        if name not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    statements = '''
    CREATE TABLE IF NOT EXISTS project_mcp_resources (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        binding_id TEXT NOT NULL REFERENCES gateway_bindings(id) ON DELETE CASCADE,
        PRIMARY KEY(project_id,binding_id));
    CREATE TABLE IF NOT EXISTS client_connection_requests (
        space_id TEXT NOT NULL, user_id TEXT NOT NULL, request_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, grant_id TEXT NOT NULL REFERENCES grants(id),
        PRIMARY KEY(space_id,user_id,request_key));
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, space_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        connection_key TEXT NOT NULL, grant_id TEXT, profile_id TEXT,
        platform TEXT NOT NULL, conversation_identifier TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '', original_url TEXT NOT NULL DEFAULT '',
        first_activity REAL NOT NULL, last_activity REAL NOT NULL,
        UNIQUE(space_id,owner_user_id,connection_key,platform,conversation_identifier));
    CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(space_id,owner_user_id,last_activity DESC,id);
    CREATE TABLE IF NOT EXISTS conversation_resources (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        resource_type TEXT NOT NULL CHECK(resource_type IN ('project','vps','mcp')),
        resource_id TEXT NOT NULL, first_activity REAL NOT NULL, last_activity REAL NOT NULL,
        PRIMARY KEY(conversation_id,resource_type,resource_id));
    CREATE INDEX IF NOT EXISTS conversation_resource_lookup ON conversation_resources(resource_type,resource_id,conversation_id);
    CREATE TABLE IF NOT EXISTS conversation_operations (
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        operation_type TEXT NOT NULL CHECK(operation_type IN ('native','mcp')),
        operation_id TEXT NOT NULL, associated_at REAL NOT NULL,
        PRIMARY KEY(conversation_id,operation_type,operation_id));
    CREATE INDEX IF NOT EXISTS conversation_operation_lookup ON conversation_operations(operation_type,operation_id,conversation_id);
    CREATE TABLE IF NOT EXISTS audit_activity (
        id TEXT PRIMARY KEY, space_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        grant_id TEXT, conversation_id TEXT REFERENCES conversations(id),
        correlation TEXT NOT NULL, method TEXT NOT NULL, tool TEXT NOT NULL,
        action TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL, request_id TEXT,
        state TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS audit_activity_owner ON audit_activity(space_id,owner_user_id,created DESC,id);
    CREATE INDEX IF NOT EXISTS audit_activity_session ON audit_activity(conversation_id,updated DESC);
    CREATE TABLE IF NOT EXISTS audit_activity_resources (
        activity_id TEXT NOT NULL REFERENCES audit_activity(id),
        resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
        PRIMARY KEY(activity_id,resource_type,resource_id));
    CREATE TABLE IF NOT EXISTS audit_activity_receipts (
        activity_id TEXT NOT NULL REFERENCES audit_activity(id),
        operation_type TEXT NOT NULL, operation_id TEXT NOT NULL,
        relation TEXT NOT NULL CHECK(relation IN ('admitted','observed')),
        resources TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(activity_id,operation_type,operation_id,relation));
    CREATE INDEX IF NOT EXISTS audit_activity_receipt_lookup ON audit_activity_receipts(operation_type,operation_id);
    '''
    for statement in statements.split(';'):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR IGNORE INTO meta VALUES ('resource_model_schema','1')")
    if db.execute("SELECT value FROM meta WHERE key='resource_model_schema'").fetchone()[0] != '1':
        raise RuntimeError('Unsupported resource model schema')
