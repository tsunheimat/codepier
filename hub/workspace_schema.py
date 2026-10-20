"""Initialize only the workspace schema; refuse incompatible databases intact."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 'workspace-2'
SCHEMA = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')

# These are rejection signatures, never migration inputs or supported tables.
# Changing only a schema marker must not admit a mixed old/new runtime model.
RETIRED_TABLES = frozenset({'access_roles', 'role_assignments', 'role_grants',
    'source_roots', 'source_mounts', 'action_routes', 'space_route_defaults',
    'project_action_routes', 'space_folders', 'space_storage', 'space_storage_mappings', 'space_shell_settings', 'workflows', 'workflow_events', 'workflow_replays'})
RETIRED_COLUMNS = {
    'projects': frozenset({'device_id', 'root', 'routing_managed', 'source_root_id', 'folder_id'}),
    'access_profiles': frozenset({'role_id', 'user_id', 'scopes', 'projects'}),
    'grants': frozenset({'role_id', 'authorization_mode', 'scopes', 'projects'}),
    'memberships': frozenset({'level', 'role_id'}),
    'sessions': frozenset({'space_id', 'current_space_id', 'active_space_id'}),
}


def require_current_model(db, tables):
    if tables & RETIRED_TABLES:
        raise RuntimeError('WORKSPACE_DATABASE_REQUIRED: retired model tables are not supported; '
                           'use a separate empty HUB_DATA_DIR. The database was not modified.')
    for table, retired in RETIRED_COLUMNS.items():
        # Only source-controlled identifiers enter this PRAGMA.
        if table in tables and retired & {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
            raise RuntimeError('WORKSPACE_DATABASE_REQUIRED: retired model columns are not supported; '
                               'use a separate empty HUB_DATA_DIR. The database was not modified.')


def initialize(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables:
        marker = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() if 'meta' in tables else None
        if not marker or marker[0] != SCHEMA_VERSION:
            raise RuntimeError('WORKSPACE_DATABASE_REQUIRED: this build requires a new workspace database. '
                               'The existing database was not migrated or deleted. Back it up with its key '
                               'and configure a separate empty HUB_DATA_DIR; project files are not affected.')
        require_current_model(db, tables)
        return
    statement = ''
    for line in SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    if statement.strip():
        raise RuntimeError('Incomplete workspace schema statement')
    db.execute("INSERT INTO meta(key,value) VALUES('schema',?)", (SCHEMA_VERSION,))
