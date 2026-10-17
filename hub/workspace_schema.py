"""Initialize only the workspace schema; refuse incompatible databases intact."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 'workspace-1'
SCHEMA = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')


def initialize(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables:
        marker = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() if 'meta' in tables else None
        if not marker or marker[0] != SCHEMA_VERSION:
            raise RuntimeError('WORKSPACE_DATABASE_REQUIRED: this build requires a new workspace database. '
                               'The existing database was not migrated or deleted. Back it up with its key '
                               'and configure a separate empty HUB_DATA_DIR; project files are not affected.')
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
