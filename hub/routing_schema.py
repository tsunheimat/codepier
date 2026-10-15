"""Transactional shared-source routing migration; legacy mappings retain identity.

Only projects explicitly adopted into shared-source management use new routes.
An unbound logical project has no device/path fallback. No grant is expanded.
"""
from __future__ import annotations

import re

DDL = '''
CREATE TABLE IF NOT EXISTS source_roots (
    id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
    name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), created REAL NOT NULL,
    UNIQUE(space_id,name));
CREATE TABLE IF NOT EXISTS source_mounts (
    id TEXT PRIMARY KEY, source_root_id TEXT NOT NULL REFERENCES source_roots(id),
    space_id TEXT NOT NULL REFERENCES spaces(id), device_id TEXT NOT NULL REFERENCES devices(id),
    path TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('read','write')),
    allow_tasks INTEGER NOT NULL DEFAULT 0 CHECK(allow_tasks IN (0,1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), validated_at REAL NOT NULL,
    UNIQUE(source_root_id,device_id));
CREATE TABLE IF NOT EXISTS action_routes (
    id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
    name TEXT NOT NULL, family TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(id), settings TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), UNIQUE(space_id,name));
CREATE TABLE IF NOT EXISTS space_route_defaults (
    space_id TEXT NOT NULL REFERENCES spaces(id), family TEXT NOT NULL,
    route_id TEXT NOT NULL REFERENCES action_routes(id), version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(space_id,family));
CREATE TABLE IF NOT EXISTS project_action_routes (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, family TEXT NOT NULL,
    route_id TEXT REFERENCES action_routes(id), version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(project_id,family));
CREATE TABLE IF NOT EXISTS space_execution_settings (
    space_id TEXT PRIMARY KEY REFERENCES spaces(id), settings TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0));
CREATE TABLE IF NOT EXISTS project_execution_settings (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    settings TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1 CHECK(version>0));
CREATE TABLE IF NOT EXISTS routing_requests (
    space_id TEXT NOT NULL, user_id TEXT NOT NULL, request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL, result TEXT NOT NULL,
    PRIMARY KEY(space_id,user_id,request_key));
'''


def _nullable_project_location(db):
    columns = {row[1]: row for row in db.execute('PRAGMA table_info(projects)')}
    if not columns['device_id'][3] and not columns['root'][3]:
        return
    if db.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise RuntimeError('Routing migration requires the Store migration transaction with foreign keys disabled')
    original = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()[0]
    statement = re.sub(r'\b(device_id|root)(\s+TEXT)\s+NOT\s+NULL\b', r'\1\2', original, flags=re.I)
    statement = re.sub(r'^CREATE TABLE\s+(?:"projects"|projects)', 'CREATE TABLE __routing_projects', statement, count=1, flags=re.I)
    auxiliary = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='projects' AND type='index' AND sql IS NOT NULL")]
    # SQLite validates triggers on other tables during RENAME. Preserve all of
    # them inside the migration transaction while projects is temporarily absent.
    triggers = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"))
    for name, _ in triggers:
        db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    db.execute(statement)
    names = ','.join('"' + name + '"' for name in columns)
    db.execute(f'INSERT INTO __routing_projects({names}) SELECT {names} FROM projects')
    db.execute('DROP TABLE projects')
    db.execute('ALTER TABLE __routing_projects RENAME TO projects')
    for sql in auxiliary:
        db.execute(sql)
    for _, sql in triggers:
        db.execute(sql)


def migrate(db):
    marker = db.execute("SELECT value FROM meta WHERE key='project_routing_schema'").fetchone()
    if marker:
        if marker[0] != '1':
            raise RuntimeError('Unsupported project routing schema')
        return
    _nullable_project_location(db)
    for statement in DDL.split(';'):
        if statement.strip():
            db.execute(statement)
    additions = (
        ('projects', 'routing_managed', 'INTEGER NOT NULL DEFAULT 0 CHECK(routing_managed IN (0,1))'),
        ('projects', 'source_root_id', 'TEXT REFERENCES source_roots(id)'),
        ('projects', 'subpath', "TEXT NOT NULL DEFAULT '.'"),
        ('projects', 'routing_version', 'INTEGER NOT NULL DEFAULT 1'),
        ('grants', 'route_ceiling', 'TEXT'),
        ('operations', 'route_context', 'TEXT'),
        ('operations', 'source_lock_released', 'INTEGER NOT NULL DEFAULT 0'),
    )
    for table, name, definition in additions:
        if name not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    db.execute('CREATE INDEX IF NOT EXISTS source_mounts_space ON source_mounts(space_id,source_root_id,device_id)')
    db.execute('CREATE INDEX IF NOT EXISTS projects_source ON projects(space_id,source_root_id,subpath)')
    db.execute("INSERT INTO meta(key,value) VALUES('project_routing_schema','1')")
