"""Permanent Hub device identity and lifespan-owned, ordinary Agent supervision.

The marker is authority, not Agent-reported info. Keep it after device deletion;
invalid state is never an invitation to enroll a replacement identity.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import time
import uuid

import anyio

from hub.builtin_roots import load_settings
from shared.local_transport import PipeTransport
from shared.util import DevError, atomic_json

log = logging.getLogger(__name__)
META_KEY = "builtin_agent"
BASE = Path(__file__).resolve().parent.parent


def _marker(store):
    row = store.one("SELECT value FROM meta WHERE key=?", (META_KEY,))
    if row is None:
        return None
    try:
        marker = json.loads(row["value"])
        if not isinstance(marker, dict) or type(marker.get("version")) is not int or marker["version"] != 1:
            raise ValueError
        for key in ("device_id", "owner_user_id", "space_id"):
            if not isinstance(marker.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", marker[key]):
                raise ValueError
        return marker
    except (ValueError, TypeError, RecursionError):
        raise DevError("BUILTIN_AGENT_STATE", "Hub 内置设备身份记录无效；请恢复原始记录，不会自动重新注册", 409) from None


def builtin_device_id(store):
    marker = _marker(store)
    return marker["device_id"] if marker else None


def is_builtin_device(store, device_id):
    identifier = builtin_device_id(store)
    return identifier is not None and identifier == device_id


def require_external_device(store, device_id):
    if is_builtin_device(store, device_id):
        raise DevError("BUILTIN_AGENT_MANAGED", "Hub 内置设备由 Hub 部署管理，不支持独立安装、更新、重启或卸载", 409)


def ensure_identity(store):
    """Enroll once, atomically with original ownership and its audit record."""
    with store.transaction():
        marker = _marker(store)
        if marker is not None:
            return marker
        owner = store.one("""SELECT u.id AS owner_user_id,s.id AS space_id
            FROM users u JOIN iam_users i ON i.user_id=u.id
            JOIN spaces s ON s.id=i.personal_space_id
            WHERE i.active=1 AND i.instance_admin=1 AND s.active=1 AND s.kind='personal'
            AND EXISTS(SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.space_id=s.id
                AND m.active=1 AND m.level='owner' AND (m.expires IS NULL OR m.expires>?))
            AND NOT EXISTS(SELECT 1 FROM membership_blocks b WHERE b.user_id=u.id AND b.space_id=s.id AND b.blocked=1)
            ORDER BY u.created,u.id,s.id LIMIT 1""", (time.time(),))
        if owner is None:
            return None
        marker = {"version": 1, "device_id": "dev_" + uuid.uuid4().hex, **owner}
        store.db.execute("INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES(?,?,?,?,?,?)",
                         (marker["device_id"], "CodePier Hub", store.encrypt(secrets.token_urlsafe(48)), time.time(),
                          marker["space_id"], marker["owner_user_id"]))
        store.db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (META_KEY, json.dumps(marker)))
        store.db.execute("INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id) VALUES(?,?,?,?,?,?,?,?)",
                         (time.time(), "hub:builtin-agent", "device.builtin_enrolled", marker["device_id"], "ok", "{}",
                          marker["space_id"], marker["owner_user_id"]))
        return marker


def listener_url(config):
    """Use the effective local listener, never an advertised public/MCP origin."""
    host = config.host.strip("[]")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    return f"http://{'[' + host + ']' if ':' in host else host}:{config.port}"


async def _drain(task):
    """Finish an owned cleanup/spawn even under repeated ASGI cancellation."""
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
    return task.result()


class BuiltinAgent:
    def __init__(self, store, runtime, config, *, process_factory=None, clock=None,
                 interval=1.0, terminate_timeout=5.0):
        self.store, self.runtime, self.config = store, runtime, config
        self.directory = store.directory / "builtin-agent"
        self.config_path = self.directory / "config.json"
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.clock = clock or time.monotonic
        self.interval, self.terminate_timeout = interval, terminate_timeout
        self.process = None
        self._transport = None
        self._session = None
        self._task = None
        self._loop = None
        self._wake = asyncio.Event()
        self._current = None
        self._failures = 0
        self._next_start = 0.0
        self._started_at = 0.0

    async def start(self):
        if not self.config.builtin_agent_enabled or self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="hub-builtin-agent")

    def wake(self):
        # Device writes can commit on Store's worker thread.
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    async def stop(self):
        task = self._task
        if task is None:
            return
        task.cancel()
        cleanup = asyncio.ensure_future(asyncio.gather(task, return_exceptions=True))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await _drain(cleanup)
            raise
        finally:
            self._task = None
            self._loop = None

    def _snapshot(self):
        # Lazy import: IAM imports Runtime, whose admission guards import us.
        from hub.iam import device_identity_active
        with self.store.transaction():
            before = builtin_device_id(self.store)
            marker = ensure_identity(self.store)
            if before is None and marker is not None and self.runtime is not None:
                self.runtime.publish("device", {"id": marker["device_id"]})
            if marker is None:
                return None
            device = self.store.one("SELECT * FROM devices WHERE id=?", (marker["device_id"],))
            if device is None:  # Permanent deletion tombstone.
                return None
            if any(device[key] != marker[key] for key in ("owner_user_id", "space_id")):
                raise DevError("BUILTIN_AGENT_STATE", "Hub 内置设备原始归属不一致；不会重新分配", 409)
            if not device_identity_active(self.store, device):
                return None
            settings = load_settings(self.store)
            return {"device_id": device["id"], "name": device["name"], "secret": self.store.decrypt(device["secret"]),
                    "revision": settings["revision"], "roots": settings["roots"]}

    def _write_config(self, identity):
        from agent.config import validate_config
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        state = self.directory / "state"
        state.mkdir(mode=0o700, exist_ok=True)
        os.chmod(state, 0o700)
        config = validate_config({**{key: identity[key] for key in ("device_id", "name", "secret")},
                                  "hub_url": listener_url(self.config), "state_dir": str(state),
                                  "allowed_roots": identity["roots"], "tasks": {}, "shell": {"enabled": False},
                                  "builtin_root_policy": {"protected_paths": [str(self.store.directory.resolve())],
                                                          "roots": [root["path"] for root in identity["roots"]]},
                                  "builtin_revision": identity["revision"],
                                  "skills": {"codex_enabled": False, "extra_roots": []},
                                  "computer": {"enabled": False}, "integrations": {
                                      "language_servers": {}, "browser": {"enabled": False}, "local_control": False}},
                                 self.config_path)
        atomic_json(self.config_path, config)

    def _failed(self):
        self._failures = min(self._failures + 1, 6)
        self._next_start = self.clock() + min(2 ** (self._failures - 1), 30)

    async def _reap(self):
        process, self.process = self.process, None
        transport, self._transport = self._transport, None
        session, self._session = self._session, None
        self._current = None
        if session is not None:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)

        async def discard_output():
            while await process.stdout.read(64 * 1024):
                pass

        # A paused, full stdout pipe can otherwise keep Process.wait pending even
        # after the child exits. The protocol reader has finished before draining.
        draining = asyncio.create_task(discard_output()) if process is not None else None
        try:
            # EOF also provides a fail-safe when termination signals are unavailable.
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), self.terminate_timeout)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
        finally:
            if draining is not None:
                draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)
            if transport is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(transport.close(), 2)

    async def _stop_child(self):
        cleanup = asyncio.create_task(self._reap())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await _drain(cleanup)
            raise

    async def _spawn(self, identity):
        self._write_config(identity)
        # The bundled child needs no Hub/OIDC/provider credentials or proxy URLs.
        allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                   "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
                   "APPDATA", "LOCALAPPDATA", "USERPROFILE", "VIRTUAL_ENV"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env["PYTHONUNBUFFERED"] = "1"
        spawning = asyncio.create_task(self.process_factory(
            sys.executable, "-m", "agent", "--config", str(self.config_path), "run", "--supervised", "--local-stdio",
            cwd=str(BASE), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env))
        try:
            self.process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            try:
                self.process = await _drain(spawning)
            except Exception:
                # A failed spawn owns no child; do not turn shutdown cancellation
                # into a recoverable reconcile error and restart the loop.
                pass
            raise
        self._transport = PipeTransport(self.process.stdout, self.process.stdin)
        self._session = asyncio.create_task(
            self.runtime.agent_session(self._transport, identity["device_id"], local_revision=identity["revision"]),
            name="hub-builtin-session")
        self._session.add_done_callback(lambda _: self.wake())
        self._current = identity
        self._started_at = self.clock()

    async def reconcile(self):
        """Called by one supervisor task; DB phases never span an await/lock."""
        desired = await self.store.run(self._snapshot)
        if self.process is not None and (self.process.returncode is not None or
                                         self._session is not None and self._session.done()):
            if self.clock() - self._started_at >= 60:
                self._failures = 0
            await self._stop_child()
            self._failed()
        if self.process is not None and self._current != desired:
            await self._stop_child()
            # A rotation/disable/delete can commit while child shutdown awaits.
            desired = await self.store.run(self._snapshot)
        if desired is not None and self.process is None and self.clock() >= self._next_start:
            await self._spawn(desired)

    async def _run(self):
        try:
            while True:
                self._wake.clear()
                try:
                    await self.reconcile()
                except Exception as exc:
                    # Do not log DB contents, config, keys or exception messages.
                    log.warning("Built-in Agent reconciliation failed (%s)", type(exc).__name__)
                    await self._stop_child()
                    self._failed()
                try:
                    await asyncio.wait_for(self._wake.wait(), self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop_child()
