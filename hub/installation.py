"""Read-only installation admission: empty storage or a complete current Hub.

This probe never constructs Store, creates keys, changes SQLite journal settings,
or attempts migration. It runs inside the selected image with /app/data mounted
read-only before an installer performs initialization or starts services.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from hub.workspace_schema import SCHEMA_VERSION, initialize, require_current_model

EMPTY = 3


def _signature(path):
    try:
        state=path.lstat()
    except FileNotFoundError:
        return None
    import stat
    if not stat.S_ISREG(state.st_mode):
        raise RuntimeError('Installation probe refuses linked or non-regular database files')
    return (state.st_dev,state.st_ino,state.st_size,state.st_mtime_ns,state.st_ctime_ns)


@contextmanager
def database_snapshot(database):
    # SQLite WAL readers can create/update sidecars even with mode=ro:
    # https://sqlite.org/wal.html#read_only_databases
    # Open source bytes with O_RDONLY only, and let SQLite read/recover a private
    # disposable copy including the WAL. Never use immutable=1 on a live source
    # or ignore committed WAL data. Any observed source change rejects the probe.
    # This is a preflight snapshot, never a replacement or migration destination.
    paths=[database,database.with_name(database.name+'-wal'),database.with_name(database.name+'-journal')]
    before={path:_signature(path) for path in paths}
    if before[database] is None:raise RuntimeError('Database disappeared during inspection')
    with tempfile.TemporaryDirectory(prefix='codepier-install-probe-') as temporary:
        snapshot=Path(temporary)/database.name
        for path,signature in before.items():
            if signature is None:continue
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
            with os.fdopen(fd,'rb') as source:
                state=os.fstat(source.fileno())
                actual=(state.st_dev,state.st_ino,state.st_size,state.st_mtime_ns,state.st_ctime_ns)
                if actual!=signature:raise RuntimeError('Database changed during inspection; retry after current work is quiescent')
                with (Path(temporary)/path.name).open('xb') as target:
                    while chunk:=source.read(1024*1024):target.write(chunk)
        if before!={path:_signature(path) for path in paths}:
            raise RuntimeError('Database changed during inspection; retry after current work is quiescent')
        yield snapshot
        if before!={path:_signature(path) for path in paths}:
            raise RuntimeError('Database changed during inspection; no installation was admitted')


def probe(directory: Path) -> int:
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError('A real, explicitly mounted Hub data directory is required')
    database = directory / 'hub.sqlite3'
    if database.is_symlink():
        raise RuntimeError('Hub database must not be a symlink')
    if not database.exists():
        # Missing metadata must never turn keys, backups or an unrelated mounted
        # source directory into a new owner account or empty replacement Hub.
        if any(directory.iterdir()):
            raise RuntimeError('Database is absent but the data directory is not empty; nothing was initialized')
        return EMPTY
    if not database.is_file() or database.stat().st_size == 0:
        raise RuntimeError('Hub database is empty or not a regular file; restore it or use an empty data directory')
    for suffix in ('-wal', '-shm', '-journal'):
        if database.with_name(database.name + suffix).is_symlink():
            raise RuntimeError('SQLite companion files must not be symlinks')
    with database_snapshot(database) as inspected, closing(sqlite3.connect(inspected,timeout=10)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        marker = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() if 'meta' in tables else None
        if not marker or marker[0] != SCHEMA_VERSION:
            raise RuntimeError('WORKSPACE_DATABASE_REQUIRED: old or unknown databases are not migrated; use a separate empty data directory')
        require_current_model(db, tables)
        with closing(sqlite3.connect(':memory:')) as expected:
            initialize(expected)
            required = {row[0] for row in expected.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not required <= tables:
            raise RuntimeError('Current schema is incomplete; no partial database is initialized or repaired')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Database integrity checks failed; restore a complete matching database and key')
        key = directory / 'master.key'
        if key.is_symlink() or not key.is_file():
            raise RuntimeError('Current database requires its matching master.key; no replacement key was created')
        raw = key.read_bytes().strip()
        try:
            decoded = base64.b64decode(raw, altchars=b'-_', validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError('master.key is invalid; no replacement key was created') from exc
        if len(decoded) != 32 or len(raw) != 44:
            raise RuntimeError('master.key is invalid; no replacement key was created')
        if db.execute('SELECT id FROM users LIMIT 1').fetchone():
            return 0
        # A successfully created current schema may be awaiting first owner
        # setup. Reject unexplained authority before permitting that setup.
        for table in ('iam_users', 'spaces', 'memberships', 'grants', 'tokens', 'devices', 'projects'):
            if db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                raise RuntimeError('Database contains resources without an owner account; no account was added')
        return EMPTY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=os.environ.get('HUB_DATA_DIR', '/app/data'))
    args = parser.parse_args(argv)
    try:
        return probe(Path(args.data_dir))
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print('Hub data admission failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
