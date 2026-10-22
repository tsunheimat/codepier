"""Destructive VPS-only schema cutoff; no runtime legacy adapter.

On Hub startup, the one-instance lock allows an automatic, backed-up reset of
recognizable old VPS tables. The offline CLI still supports preview/confirmation.
Neither path stops remote processes, changes SSH credentials or migrates a VPS.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sqlite3
import time

from hub.workspace_schema import SCHEMA, SCHEMA_VERSION, VPS_SCHEMA_VERSION, RETIRED_TABLES, RETIRED_COLUMNS
from shared.instance_lock import InstanceLock


def statements():
    statement = ''
    for line in SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ''


def object_value(value):
    try:
        result = json.loads(value or '{}')
    except (TypeError, ValueError):
        raise RuntimeError('Malformed stored JSON; database was not changed') from None
    return result


def contains_vps(value, identifiers):
    if isinstance(value, str):
        return value in identifiers or value.startswith('vps:') and value[4:] in identifiers
    if isinstance(value, dict):
        return any(contains_vps(child, identifiers) for child in value.values())
    if isinstance(value, list):
        return any(contains_vps(child, identifiers) for child in value)
    return False


def remove_vps_policy(value):
    if isinstance(value, dict):
        return {key: [] if key == 'vps_rules' else remove_vps_policy(child) for key, child in value.items()}
    if isinstance(value, list):
        return [remove_vps_policy(child) for child in value]
    return value


def cutoff(directory, *, apply=False, confirmed_stopped=False, _hub_lock_held=False, _automatic=False):
    directory = Path(directory).expanduser().resolve(strict=True)
    path = directory / 'hub.sqlite3'
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('An existing, non-symlink Hub database is required')
    lock_path = directory / '.hub.lock'
    if lock_path.is_symlink():
        raise RuntimeError('Hub instance lock must not be a symbolic link')
    # The Hub owns .hub.lock before calling this at startup. The offline CLI
    # acquires the same lock itself. Never attempt to lock the same file twice.
    lock = nullcontext() if _hub_lock_held else InstanceLock(lock_path)
    with lock, sqlite3.connect(path, timeout=5) as db:
        db.row_factory = sqlite3.Row
        marker = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if not marker or marker[0] != SCHEMA_VERSION:
            raise RuntimeError('Only the workspace-2 VPS cutoff is supported; unrelated old workspace schemas are not migrated')
        vps_marker = db.execute("SELECT value FROM meta WHERE key='vps_schema'").fetchone()
        columns = {row[1] for row in db.execute('PRAGMA table_info(space_vps)')}
        if vps_marker and vps_marker[0] == VPS_SCHEMA_VERSION:
            resources = {row[1] for row in db.execute('PRAGMA table_info(vps_connections)')}
            if {'runner_device_id', 'connection_ref'} & columns or not {'runner_device_id', 'host_alias', 'target_fingerprint', 'target_version'} <= resources:
                raise RuntimeError('VPS schema marker contradicts its tables; database was not changed')
            return {'changed': False, 'status': 'already_current'}
        if not {'runner_device_id', 'connection_ref'} <= columns:
            raise RuntimeError('Unrecognized VPS schema; database was not changed')
        bindings = {row[0] for row in db.execute('SELECT id FROM space_vps')}
        resources = {row[0] for row in db.execute('SELECT id FROM vps_connections')}
        session_ids = {row[0] for row in db.execute('SELECT id FROM execution_sessions WHERE vps_id IS NOT NULL')}
        identifiers = bindings | resources | session_ids
        operations = []
        active = []
        for row in db.execute('SELECT id,state,authorization_context,route_context,args_summary FROM operations'):
            if any(contains_vps(object_value(row[key]), identifiers)
                   for key in ('authorization_context', 'route_context', 'args_summary')):
                operations.append(row['id'])
                if row['state'] in {'queued', 'running', 'reconnecting', 'cancelling', 'needs_review'}:
                    active.append(row['id'])
        result = {'changed': False, 'vps_resources': len(resources), 'space_bindings': len(bindings),
                  'vps_sessions': len(session_ids), 'vps_operations': len(operations),
                  'unresolved_operations': len(active), 'remote_processes_stopped_by_tool': False,
                  'project_files_deleted': False, 'schema': VPS_SCHEMA_VERSION}
        if not apply:
            return {**result, 'status': 'preview', 'confirmation_required': True}
        if not (confirmed_stopped or _automatic):
            raise RuntimeError('Explicit confirmation that old VPS processes have been resolved/stopped is required')
        # sqlite3.backup produces a consistent snapshot including WAL pages.
        backup = directory / ('vps-cutoff-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns()) + '.sqlite3')
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            db.execute('BEGIN EXCLUSIVE')
            db.execute('CREATE TEMP TABLE cutoff_operations(id TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO cutoff_operations VALUES(?)', [(identifier,) for identifier in operations])
            for table, column in [('operation_events', 'operation_id'), ('conversation_operations', 'operation_id'),
                                  ('audit_activity_receipts', 'operation_id')]:
                db.execute(f'DELETE FROM {table} WHERE {column} IN (SELECT id FROM cutoff_operations)')
            db.execute('DELETE FROM operations WHERE id IN (SELECT id FROM cutoff_operations)')
            for table in ('conversation_resources', 'audit_activity_resources'):
                db.execute(f"DELETE FROM {table} WHERE resource_type='vps'")
            for row in db.execute('SELECT rowid,resources FROM audit_activity_receipts').fetchall():
                value = object_value(row['resources'])
                filtered = [item for item in value if item.get('type') != 'vps']
                if filtered != value:
                    db.execute('UPDATE audit_activity_receipts SET resources=? WHERE rowid=?', (json.dumps(filtered), row['rowid']))
            db.execute('DELETE FROM execution_sessions WHERE vps_id IS NOT NULL')
            db.execute("DELETE FROM workspace_requests WHERE operation LIKE 'vps.%'")
            # Keep non-VPS idempotency receipts (never recreate a connection on
            # retry), but remove their obsolete VPS policy snapshots.
            for row in db.execute('SELECT rowid,receipt FROM workspace_requests').fetchall():
                value = object_value(row['receipt'])
                filtered = remove_vps_policy(value)
                if filtered != value:
                    db.execute('UPDATE workspace_requests SET receipt=? WHERE rowid=?', (json.dumps(filtered), row['rowid']))
            for table, column in [('memberships', 'policy'), ('access_profiles', 'policy'), ('grants', 'resource_policy')]:
                for row in db.execute(f'SELECT rowid,{column} FROM {table}').fetchall():
                    value = object_value(row[column])
                    filtered = remove_vps_policy(value)
                    if filtered != value:
                        version = ',version=version+1' if table == 'access_profiles' else ''
                        db.execute(f'UPDATE {table} SET {column}=?{version} WHERE rowid=?', (json.dumps(filtered), row['rowid']))
            for row in db.execute('SELECT id,target_snapshots FROM grants').fetchall():
                value = object_value(row['target_snapshots'])
                value['vps'] = {}
                db.execute('UPDATE grants SET target_snapshots=? WHERE id=?', (json.dumps(value), row['id']))
            for table in ('vps_projects', 'space_vps', 'vps_connections'):
                db.execute('DROP TABLE ' + table)
            for statement in statements():
                if statement.lstrip().startswith(('CREATE TABLE vps_connections (', 'CREATE TABLE space_vps (', 'CREATE TABLE vps_projects (')):
                    db.execute(statement)
            db.execute("INSERT INTO meta(key,value) VALUES('vps_schema',?)", (VPS_SCHEMA_VERSION,))
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('VPS cutoff would break foreign-key relationships')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute('PRAGMA foreign_keys=ON')
        return {**result, 'changed': True, 'status': 'cutoff_complete', 'backup': str(backup),
                'note': 'Use the original matching master.key with the backup. Re-register VPS resources; no old target is converted.'}



def automatic_cutoff(directory):
    """Hard-reset only the *known old* VPS layout while Hub owns .hub.lock.

    Fresh DBs, non-workspace-2 DBs and the current model are left to normal
    initialization. An unknown/mixed/future layout is never guessed or erased.
    A valid workspace-2 database with the old Space-owned VPS layout gets a
    private SQLite backup followed by the same transactional cleanup as the
    offline cutoff. No opt-in environment variable or manual confirmation.
    """
    directory = Path(directory).expanduser().resolve()
    path = directory / 'hub.sqlite3'
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Hub database must be an existing, non-symlink file')
    with sqlite3.connect(path, timeout=5) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if 'meta' not in tables:
            return None  # The normal workspace-model check will reject it.
        marker = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if not marker or marker[0] != SCHEMA_VERSION:
            return None  # Never modify another workspace schema.
        if tables & RETIRED_TABLES:
            return None
        for table, retired in RETIRED_COLUMNS.items():
            if table in tables and retired & {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
                return None
        vps_marker = db.execute("SELECT value FROM meta WHERE key='vps_schema'").fetchone()
        if vps_marker:
            return None  # Current version or unknown/mixed version: validate, do not erase.
        columns = {row[1] for row in db.execute('PRAGMA table_info(space_vps)')}
        resources = {row[1] for row in db.execute('PRAGMA table_info(vps_connections)')}
        if not {'runner_device_id', 'connection_ref'} <= columns:
            return None  # Not the known old Space-owned layout.
        if not {'host', 'provider', 'region', 'system', 'notes'} <= resources:
            return None  # Do not discard an unknown or partly rebuilt resource table.
        if {'runner_device_id', 'host_alias', 'target_fingerprint', 'target_version'} & resources:
            return None  # No automatic reset of a mixed old/new schema.
    return cutoff(directory, apply=True, _hub_lock_held=True, _automatic=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--drop-vps', action='store_true', help='Apply the destructive cutoff; otherwise preview only')
    parser.add_argument('--confirm-remote-processes-stopped', action='store_true')
    args = parser.parse_args()
    try:
        result = cutoff(args.data_dir, apply=args.drop_vps, confirmed_stopped=args.confirm_remote_processes_stopped)
        print(json.dumps(result, indent=2))
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
