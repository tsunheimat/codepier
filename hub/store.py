from __future__ import annotations
import json
from contextlib import contextmanager
import os
import sqlite3
import tempfile
import asyncio
import anyio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from hub.store_contract import CheckedConnection, OwnedRLock
import time
from pathlib import Path
from cryptography.fernet import Fernet

from hub.workspace_schema import SCHEMA

class Store:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        connection = sqlite3.connect(self.directory / "hub.sqlite3", check_same_thread=False, timeout=30)
        connection.row_factory = sqlite3.Row
        self.lock = OwnedRLock()
        self.iam_enabled = True
        self.session_revision = 0
        self._event_loop = None
        self.db = CheckedConnection(connection, self.lock, self._security_write)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-sqlite")
        with self.lock:
            try:
                # Changing a new database to WAL can return BUSY without invoking
                # SQLite's busy handler when another opener is doing the same.
                deadline = time.monotonic() + 30
                while True:
                    try:
                        self.db.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                self.db.execute("PRAGMA foreign_keys=OFF")
                with self.db:
                    # Serialize the reads that decide which migrations/key creation
                    # are needed with all writes made by other Store instances.
                    self.db.execute("BEGIN IMMEDIATE")
                    self._migrate()
                    self.cipher = self._load_cipher()
                    if self.db.execute("PRAGMA foreign_key_check").fetchall():
                        raise RuntimeError("Hub migration has invalid foreign-key relationships")
                self.db.execute("PRAGMA foreign_keys=ON")
                os.chmod(self.directory / "hub.sqlite3", 0o600)
            except BaseException:
                self.db.close()
                raise

    def _load_cipher(self):
        key_path = self.directory / "master.key"
        if not key_path.exists():
            from hub.keyring import CIPHER_COLUMNS
            for table, column in CIPHER_COLUMNS:
                columns = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if column in columns and self.db.execute(
                    f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL LIMIT 1"
                ).fetchone():
                    raise RuntimeError("master.key is missing for an encrypted Hub database; restore the matching master.key from backup")
            fd, temporary = tempfile.mkstemp(prefix=".master-key-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Publish a complete, durable file without replacing an
                    # existing key, including one installed by an administrator.
                    os.link(temporary, key_path)
                except FileExistsError:
                    pass
                if os.name != "nt":
                    directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                os.unlink(temporary)
        from hub.keyring import CipherSuite
        return CipherSuite(self.directory, key_path.read_bytes().strip())

    def _migrate(self):
        from hub.workspace_schema import initialize
        initialize(self.db)

    @contextmanager
    def transaction(self, *, immediate=True):
        """Join the checked outer context; any nested failure is rollback-only."""
        with self.lock, self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
            yield self.db

    def execute(self, sql: str, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args)


    def one(self, sql: str, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def encrypt(self, value: str):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str):
        return self.cipher.decrypt(value.encode()).decode()

    def audit(self, actor: str, action: str, target: str = "", status: str = "ok", detail=None, *, commit=True, target_kind=None):
        from hub.iam import audit_context
        context = audit_context.get()
        # Context is trusted only for its originating Store AND actor. A task
        # can outlive the HTTP request, and direct callers may use another Store.
        scoped = bool(context and context[0] is self and context[1] == actor)
        sid, uid = context[2:] if scoped else (None, None)
        if not scoped and actor.startswith('mcp:'):
            grant = self.one('SELECT space_id,user_id FROM grants WHERE id=?', (actor.split(':', 2)[1],))
            if grant: sid, uid = grant['space_id'], grant['user_id']
        elif actor.startswith('panel:') and uid is None:
            user = self.one('SELECT id FROM users WHERE username=?', (actor[6:],))
            if user:
                uid = user['id']
        # Do not retarget a scoped caller's audit entry using an untrusted ID
        # from a denied cross-Space request. Also avoid loading operation bodies
        # just to audit a thin status read.
        tables = {'operation': 'operations', 'project': 'projects', 'device': 'devices',
                  'profile': 'access_profiles',
                  'artifact': 'artifacts', 'grant': 'grants', 'vps': 'vps_connections'}
        if target_kind is not None and target_kind not in tables:
            raise ValueError('Unknown audit target kind')
        # A verified caller scope (including MCP) takes precedence even when the
        # target is an ID in another Space. Free-form names/URLs never cause lookup.
        if target_kind and target and not scoped and not actor.startswith('mcp:'):
            row = self.one(f'SELECT space_id,owner_user_id FROM {tables[target_kind]} WHERE id=?', (target,))
            if row:
                sid = row['space_id']
        sql = 'INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(?,?,?,?,?,?,?,?)'
        args = (time.time(), actor, action, target, status, json.dumps(detail or {}, ensure_ascii=False), sid, uid)
        if commit:
            self.execute(sql, args)
        else:
            self.db.execute(sql, args)

    def _security_write(self, sql):
        # Conservative invalidation is safe even if the transaction rolls back.
        # No SQL text or bound value is retained in this process-local revision.
        import re
        if re.search(r"\b(?:sessions|session_security|users|iam_users|spaces|memberships|membership_blocks|external_identities|oidc_providers|access_profiles|grants|tokens|projects|devices|external_integrations|project_integrations|external_plugin_consents|space_devices|storage_pools|storage_mappings|space_storage|space_storage_mappings|space_folders|space_vps|vps_projects|space_shell_settings|project_shell_settings|space_execution_settings|project_execution_settings|gateway_[a-z_]+)\b", sql, re.I):
            self.session_revision += 1

    def after_commit(self, callback):
        if self.lock.owned:
            self.db.after_commit(callback)
        else:
            callback()

    def require_transaction(self):
        self.lock.require()
        if not self.db.depth:
            raise RuntimeError("This helper requires an enclosing Store database context")

    async def run(self, function, /, *args, **kwargs):
        """Run a whole synchronous DB phase on this Store's single worker.

        Cancellation waits for the admitted phase to settle before releasing the
        caller's async admission lock. No transaction or thread lock crosses an
        await; cancellation is not a claim that a durable write did not happen.
        """
        if self.lock.owned:
            raise RuntimeError("Do not await Store.run while holding Store.lock")
        self._event_loop = asyncio.get_running_loop()
        context = copy_context()
        call = partial(function, *args, **kwargs)
        future = asyncio.get_running_loop().run_in_executor(self._executor, context.run, call)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # ASGI uses AnyIO level cancellation: an already-cancelled scope
            # cancels every new await. Shield the drain so it can suspend instead
            # of spinning, while still tolerating explicit asyncio Task.cancel().
            with anyio.CancelScope(shield=True):
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            # Retrieve a failing job's exception without masking cancellation.
            if not future.cancelled():
                future.exception()
            raise

    async def aclose(self):
        await asyncio.to_thread(self.close)

    def close(self):
        if self.lock.owned:
            raise RuntimeError("Release Store.lock before closing the database worker")
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self.lock:
            self.db.close()
