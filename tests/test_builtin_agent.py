"""Unit contracts for permanent enrollment and managed child ownership."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from hub.builtin_agent import (BuiltinAgent, META_KEY, builtin_device_id, ensure_identity,
                               is_builtin_device, listener_url, require_external_device)
from hub.config import HubConfig
from hub.store import Store
from shared.util import DevError


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "hub")
    try:
        yield value
    finally:
        value.close()


def admin(store, identifier="admin", created=1, *, active=True, instance_admin=True):
    store.execute("INSERT INTO users VALUES(?,?,?,?)", (identifier, identifier, "!oidc-only", created))
    store.execute("UPDATE iam_users SET instance_admin=?,active=? WHERE user_id=?", (int(instance_admin), int(active), identifier))
    return store.one("SELECT personal_space_id FROM iam_users WHERE user_id=?", (identifier,))["personal_space_id"]


def config(**kwargs):
    return replace(HubConfig.from_env(), builtin_agent_enabled=True, **kwargs)


def test_default_on_and_explicit_optout(monkeypatch):
    monkeypatch.delenv("HUB_BUILTIN_AGENT_ENABLED", raising=False)
    assert HubConfig.from_env().builtin_agent_enabled
    monkeypatch.setenv("HUB_BUILTIN_AGENT_ENABLED", "false")
    assert not HubConfig.from_env().builtin_agent_enabled


def test_empty_then_late_admin_and_stable_identity(store):
    assert ensure_identity(store) is None
    space = admin(store)
    marker = ensure_identity(store)
    device = store.one("SELECT * FROM devices WHERE id=?", (marker["device_id"],))
    assert marker["owner_user_id"] == "admin" and marker["space_id"] == space
    assert device["name"] == "CodePier Hub" and len(store.decrypt(device["secret"])) >= 40
    assert ensure_identity(store) == marker
    assert store.one("SELECT * FROM devices WHERE id=?", (marker["device_id"],)) == device
    assert len(store.all("SELECT * FROM audit WHERE action='device.builtin_enrolled'")) == 1


def test_earliest_eligible_admin_and_stable_tie_break(store):
    admin(store, "inactive", 0, active=False)
    admin(store, "ordinary", 0, instance_admin=False)
    blocked = admin(store, "blocked", 0)
    store.execute("INSERT INTO membership_blocks(space_id,user_id) VALUES(?,?)", (blocked, "blocked"))
    admin(store, "z", 1)
    expected_space = admin(store, "a", 1)
    admin(store, "later", 2)
    marker = ensure_identity(store)
    assert marker["owner_user_id"] == "a" and marker["space_id"] == expected_space


@pytest.mark.parametrize("revocation", ["space", "membership", "expired"])
def test_initial_enrollment_requires_active_owner_membership(store, revocation):
    space = admin(store)
    if revocation == "space":
        store.execute("UPDATE spaces SET active=0 WHERE id=?", (space,))
    elif revocation == "membership":
        store.execute("UPDATE memberships SET level='member' WHERE space_id=?", (space,))
    else:
        store.execute("UPDATE memberships SET expires=1 WHERE space_id=?", (space,))
    assert ensure_identity(store) is None


def test_atomic_rollback(store):
    admin(store)
    store.execute("CREATE TRIGGER reject_builtin_marker BEFORE INSERT ON meta WHEN NEW.key='builtin_agent' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(Exception, match="test"):
        ensure_identity(store)
    assert not store.all("SELECT * FROM devices")
    assert not store.all("SELECT * FROM audit WHERE action='device.builtin_enrolled'")
    assert builtin_device_id(store) is None


def test_marker_persistence_tombstone_and_guards(store):
    admin(store)
    marker = ensure_identity(store)
    identifier = marker["device_id"]
    store.execute("DELETE FROM devices WHERE id=?", (identifier,))
    admin(store, "new-admin", 0)
    assert ensure_identity(store) == marker
    assert not store.all("SELECT * FROM devices")
    assert is_builtin_device(store, identifier)
    assert not is_builtin_device(store, "remote")
    require_external_device(store, "remote")
    with pytest.raises(DevError) as exc:
        require_external_device(store, identifier)
    assert exc.value.code == "BUILTIN_AGENT_MANAGED" and exc.value.status == 409
    reopened = Store(store.directory)
    try:
        assert ensure_identity(reopened) == marker
    finally:
        reopened.close()


@pytest.mark.parametrize("value", ["invalid", "null", "{}", '{"version":true}', '{"version":2}',
                                    '{"version":1,"device_id":"x","owner_user_id":"x","space_id":""}'])
def test_corrupt_marker_fails_closed(store, value):
    admin(store)
    store.execute("INSERT INTO meta VALUES(?,?)", (META_KEY, value))
    for operation in (lambda: ensure_identity(store), lambda: builtin_device_id(store),
                      lambda: require_external_device(store, "anything")):
        with pytest.raises(DevError) as exc:
            operation()
        assert exc.value.code == "BUILTIN_AGENT_STATE"
    assert not store.all("SELECT * FROM devices")


@pytest.mark.parametrize("host,expected", [("0.0.0.0", "127.0.0.1"), ("::", "[::1]"),
                                           ("::1", "[::1]"), ("[::1]", "[::1]"),
                                           ("127.0.0.2", "127.0.0.2"), ("localhost", "localhost")])
def test_local_listener_address(host, expected):
    assert listener_url(config(host=host, port=9123)) == f"http://{expected}:9123"


class Process:
    def __init__(self, *, stubborn=False):
        self.returncode = None
        self.closed = False
        self.terminated = 0
        self.killed = 0
        self.stubborn = stubborn
        self.exited = asyncio.Event()
        self.stdin = SimpleNamespace(close=self.close)
        self.on_terminate = None

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated += 1
        if self.on_terminate:
            self.on_terminate()
        if not self.stubborn:
            self.exit(-15)

    def kill(self):
        self.killed += 1
        self.exit(-9)

    def exit(self, code):
        self.returncode = code
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


class Factory:
    def __init__(self):
        self.children = []
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        child = Process()
        self.children.append(child)
        return child


@pytest.mark.asyncio
async def test_optout_neither_enrolls_nor_spawns(store):
    admin(store)
    factory = Factory()
    supervisor = BuiltinAgent(store, None, replace(config(), builtin_agent_enabled=False), process_factory=factory)
    await supervisor.start()
    await supervisor.stop()
    assert not factory.children and builtin_device_id(store) is None
    marker = ensure_identity(store)
    await supervisor.start()
    assert is_builtin_device(store, marker["device_id"])
    await supervisor.stop()


@pytest.mark.asyncio
async def test_private_safe_config_and_spawn_contract(store, monkeypatch):
    monkeypatch.setenv("CODEPIER_OIDC_CLIENT_SECRET", "never-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "never-inherit")
    monkeypatch.setenv("HTTPS_PROXY", "http://secret:password@proxy")
    admin(store)
    factory = Factory()
    supervisor = BuiltinAgent(store, None, config(host="127.0.0.2", port=9321), process_factory=factory)
    try:
        await supervisor.reconcile()
        value = json.loads(supervisor.config_path.read_text())
        assert value["hub_url"] == "http://127.0.0.2:9321"
        assert value["allowed_roots"] == [] and value["tasks"] == {}
        assert not value["shell"]["enabled"] and not value["computer"]["enabled"]
        assert not value["skills"]["codex_enabled"] and not value["skills"]["extra_roots"]
        assert not value["integrations"]["local_control"] and not value["integrations"]["browser"]["enabled"]
        assert value["integrations"]["language_servers"] == {}
        assert supervisor.config_path.stat().st_mode & 0o777 == 0o600
        assert supervisor.directory.stat().st_mode & 0o777 == 0o700
        args, kwargs = factory.calls[0]
        assert args[-3:] == ("run", "--supervised", "--parent-stdin")
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        assert "stdout" not in kwargs and "stderr" not in kwargs
        assert not {"CODEPIER_OIDC_CLIENT_SECRET", "OPENAI_API_KEY", "HTTPS_PROXY"} & kwargs["env"].keys()
        assert value["secret"] not in args
    finally:
        await supervisor._stop_child()


@pytest.mark.asyncio
async def test_rotation_and_concurrent_disable_reread_after_reap(store):
    admin(store)
    factory = Factory()
    supervisor = BuiltinAgent(store, None, config(), process_factory=factory)
    try:
        await supervisor.reconcile()
        child = supervisor.process
        identifier = builtin_device_id(store)
        store.execute("UPDATE devices SET secret=? WHERE id=?", (store.encrypt("s" * 64), identifier))
        await supervisor.reconcile()
        assert child.terminated == 1 and child.closed
        assert supervisor.process is not child
        assert json.loads(supervisor.config_path.read_text())["secret"] == "s" * 64
        child = supervisor.process
        child.on_terminate = lambda: store.execute("UPDATE devices SET enabled=0 WHERE id=?", (identifier,))
        store.execute("UPDATE devices SET secret=? WHERE id=?", (store.encrypt("t" * 64), identifier))
        await supervisor.reconcile()
        assert supervisor.process is None and len(factory.children) == 2
        store.execute("UPDATE devices SET enabled=1 WHERE id=?", (identifier,))
        await supervisor.reconcile()
        assert len(factory.children) == 3
        assert json.loads(supervisor.config_path.read_text())["secret"] == "t" * 64
    finally:
        await supervisor._stop_child()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["owner", "space", "membership", "block", "delete", "ownership"])
async def test_authorization_revocation_stops_without_reassignment(store, revocation):
    space = admin(store)
    factory = Factory()
    supervisor = BuiltinAgent(store, None, config(), process_factory=factory)
    await supervisor.start()
    try:
        for _ in range(200):
            if factory.children:
                break
            await asyncio.sleep(.005)
        assert len(factory.children) == 1
        identifier = builtin_device_id(store)
        if revocation == "owner":
            store.execute("UPDATE iam_users SET active=0 WHERE user_id='admin'")
        elif revocation == "space":
            store.execute("UPDATE spaces SET active=0 WHERE id=?", (space,))
        elif revocation == "membership":
            store.execute("UPDATE memberships SET active=0 WHERE space_id=?", (space,))
        elif revocation == "block":
            store.execute("INSERT INTO membership_blocks(space_id,user_id) VALUES(?,'admin')", (space,))
        elif revocation == "delete":
            store.execute("DELETE FROM devices WHERE id=?", (identifier,))
        else:
            admin(store, "other")
            store.execute("UPDATE devices SET owner_user_id='other' WHERE id=?", (identifier,))
        supervisor.wake()
        for _ in range(200):
            if factory.children[0].terminated:
                break
            await asyncio.sleep(.005)
        assert factory.children[0].terminated == 1
        assert len(factory.children) == 1 and builtin_device_id(store) == identifier
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_crash_backoff_is_bounded_and_does_not_preempt_agent_reconnect(store):
    admin(store)
    now = [100.0]
    factory = Factory()
    supervisor = BuiltinAgent(store, None, config(), process_factory=factory, clock=lambda: now[0])
    try:
        await supervisor.reconcile()
        for delay in (1, 2, 4, 8, 16, 30, 30):
            child = supervisor.process
            await supervisor.reconcile()
            assert supervisor.process is child  # No socket-readiness prerequisite.
            child.exit(1)  # Includes occupied state-lock failures.
            await supervisor.reconcile()
            assert supervisor.process is None
            assert supervisor._next_start == now[0] + delay
            now[0] += delay
            await supervisor.reconcile()
            assert supervisor.process is not None
    finally:
        await supervisor._stop_child()


@pytest.mark.asyncio
async def test_start_returns_before_spawn_and_cancelled_spawn_is_reaped(store):
    admin(store)
    spawning, release = asyncio.Event(), asyncio.Event()
    child = Process(stubborn=True)

    async def factory(*args, **kwargs):
        spawning.set()
        await release.wait()
        return child

    supervisor = BuiltinAgent(store, None, config(), process_factory=factory, terminate_timeout=.01)
    await asyncio.wait_for(supervisor.start(), .1)
    await asyncio.wait_for(spawning.wait(), 2)
    stopping = asyncio.create_task(supervisor.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    stopping.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert child.closed and child.terminated == 1 and child.killed == 1
    assert child.returncode is not None and supervisor.process is None


@pytest.mark.asyncio
async def test_failed_spawn_during_cancellation_preserves_shutdown(store):
    admin(store)
    spawning, release = asyncio.Event(), asyncio.Event()

    async def factory(*args, **kwargs):
        spawning.set()
        await release.wait()
        raise OSError("spawn failed")

    supervisor = BuiltinAgent(store, None, config(), process_factory=factory)
    await supervisor.start()
    await asyncio.wait_for(spawning.wait(), 2)
    stopping = asyncio.create_task(supervisor.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(stopping, 2)
    assert supervisor.process is None and supervisor._task is None


@pytest.mark.asyncio
async def test_spawn_failure_and_corrupt_marker_do_not_take_down_supervisor(store):
    admin(store)
    factory = Factory()
    attempts = []

    async def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("unavailable interpreter")
        return await factory(*args, **kwargs)

    now = [0.0]
    supervisor = BuiltinAgent(store, None, config(), process_factory=flaky, clock=lambda: now[0], interval=.01)
    await supervisor.start()
    try:
        for _ in range(200):
            if supervisor._failures:
                break
            await asyncio.sleep(.005)
        assert supervisor._failures == 1 and not factory.children
        assert not supervisor._task.done()
        now[0] = 1.0
        supervisor.wake()
        for _ in range(200):
            if factory.children:
                break
            await asyncio.sleep(.005)
        assert len(factory.children) == 1
        store.execute("UPDATE meta SET value='corrupt' WHERE key=?", (META_KEY,))
        supervisor.wake()
        for _ in range(200):
            if factory.children[0].terminated:
                break
            await asyncio.sleep(.005)
        assert factory.children[0].terminated == 1 and not supervisor._task.done()
        assert len(store.all("SELECT * FROM devices")) == 1
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_existing_name_disabled_state_and_secret_are_preserved(store):
    admin(store)
    marker = ensure_identity(store)
    store.execute("UPDATE devices SET name='Renamed',enabled=0 WHERE id=?", (marker["device_id"],))
    before = store.one("SELECT * FROM devices WHERE id=?", (marker["device_id"],))
    factory = Factory()
    supervisor = BuiltinAgent(store, None, config(), process_factory=factory)
    await supervisor.reconcile()
    assert not factory.children
    assert store.one("SELECT * FROM devices WHERE id=?", (marker["device_id"],)) == before
    assert ensure_identity(store) == marker


@pytest.mark.asyncio
async def test_periodic_late_bootstrap_without_wake(store):
    factory = Factory()
    supervisor = BuiltinAgent(store, None, config(), process_factory=factory, interval=.01)
    await supervisor.start()
    try:
        await asyncio.sleep(.03)
        assert not factory.children
        admin(store)
        for _ in range(200):
            if factory.children:
                break
            await asyncio.sleep(.005)
        assert len(factory.children) == 1
    finally:
        await supervisor.stop()
