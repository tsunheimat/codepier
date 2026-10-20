"""Runtime recovery, connection fencing and fault-containment regressions."""
from __future__ import annotations

import asyncio
import gc
import json
import time
import weakref

import pytest
from fastapi import WebSocketDisconnect

from hub.runtime import Connection, Principal, Runtime
from hub.store import Store
from hub import iam
from hub.permissions import owner_policy
from shared.tool_protocol import advertisement, catalog_digest
from shared.crypto import SecureChannel, token
from shared.util import DevError


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "hub")
    secret = token()
    root=tmp_path/'project'; root.mkdir()
    now=time.time()
    with store.transaction():
        store.db.execute("INSERT INTO users VALUES('owner','owner','!fixture-only',?)",(now,))
        iam.create_personal_space(store,'owner','Runtime account')
        store.db.execute("INSERT INTO spaces(id,label,owner_user_id,created) VALUES('runtime-space','Runtime','owner',?)",(now,))
        store.db.execute("INSERT INTO memberships(space_id,user_id,policy) VALUES('runtime-space','owner',?)",
                         (owner_policy().model_dump_json(),))
        store.db.execute("INSERT INTO devices(id,name,secret,created,owner_user_id,info) VALUES('dev','home',?,?,'owner',?)",
            (store.encrypt(secret),now,json.dumps({'shell_enabled':True})))
        store.db.execute("INSERT INTO space_devices(space_id,device_id) VALUES('runtime-space','dev')")
        from tests.current_model import CurrentModel
        CurrentModel(store).folder('runtime-folder',space='runtime-space',device='dev',path=root,mode='write',allow_tasks=True)
        store.db.execute("""INSERT INTO projects(id,alias,alias_key,source_id,subpath,description,created,space_id,owner_user_id)
            VALUES('proj','Project','project','runtime-folder','.','',?,'runtime-space','owner')""",(now,))
    runtime = Runtime(store)
    runtime.wait_seconds = 0
    principal = Principal('panel:owner','owner',{'read','write','execute'},['proj'],admin=True,space_id='runtime-space')
    yield runtime, principal, secret
    store.close()


class Socket:
    def __init__(self, secret, hello=None, before_hello=None, after_ready=None):
        self.secret = secret
        self.hello = hello or {"type": "hello", "delivery_protocol": 2, "journal_id": "journal-one",
            "workspace_protocol":1,"project_routing_protocol":1,"shell_enabled":True,
            **advertisement(catalog_digest())}
        self.before_hello, self.after_ready = before_hello, after_ready
        self.incoming = asyncio.Queue()
        self.receiving = asyncio.Event()
        self.acknowledgements = asyncio.Queue()
        self.packets = []
        self.closed = False
        self.first = True

    async def accept(self):
        pass

    async def send_json(self, packet):
        self.channel = SecureChannel(self.secret, packet["challenge"], "dev", "agent")

    async def send_text(self, packet):
        data = self.channel.unpack(packet)
        self.packets.append(data)
        if data["type"] == "ack":
            self.acknowledgements.put_nowait(data)
        if data["type"] == "ready" and self.after_ready:
            self.after_ready()

    async def receive_text(self):
        if self.first:
            self.first = False
            if self.before_hello:
                self.before_hello()
            return self.channel.pack(self.hello)
        self.receiving.set()
        item = await self.incoming.get()
        if item is None:
            raise WebSocketDisconnect()
        return self.channel.pack(item)

    async def close(self, **kwargs):
        self.closed = True
        self.incoming.put_nowait(None)

    async def wait_for(self, awaitable, reader):
        # DB phases run on Store's worker. Wait for actual socket progress,
        # without busy-polling or imposing a one-second latency requirement.
        waiter = asyncio.create_task(awaitable)
        try:
            done, _ = await asyncio.wait({waiter, reader}, timeout=10,
                                         return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                await reader
                pytest.fail("Agent socket exited before the fixture exchange completed")
            if waiter not in done:
                pytest.fail(f"Agent socket exchange timed out; packets={self.packets!r}")
            return waiter.result()
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 1)


@pytest.mark.asyncio
async def test_failed_send_fences_already_waiting_sender():
    started, release = asyncio.Event(), asyncio.Event()

    class FailedSocket:
        calls = 0
        async def send_text(self, packet):
            self.calls += 1
            started.set()
            await release.wait()
            raise OSError("link lost")
        async def close(self, **kwargs):
            pass

    socket = FailedSocket()
    channel = SecureChannel(token(), token(), "dev", "hub")
    connection = Connection(socket, channel)
    first = asyncio.create_task(connection.send({"type": "one"}))
    await started.wait()
    second = asyncio.create_task(connection.send({"type": "two"}))
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(item, OSError) for item in outcomes)
    assert connection.unusable and socket.calls == channel.sent == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["hello", "ready"])
@pytest.mark.parametrize("change", ["disabled", "rotated", "removed"])
async def test_credentials_revoked_during_handshake_cannot_register(runtime, phase, change):
    r, _, secret = runtime
    def revoke():
        if change == "disabled":
            r.store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
        elif change == "rotated":
            r.store.execute("UPDATE devices SET secret=? WHERE id='dev'", (r.store.encrypt(token()),))
        else:
            with r.store.transaction():
                r.store.db.execute('DELETE FROM projects')
                r.store.db.execute('DELETE FROM space_sources')
                r.store.db.execute('DELETE FROM space_devices')
                r.store.db.execute('DELETE FROM devices')
    socket = Socket(secret, **{"before_hello" if phase == "hello" else "after_ready": revoke})
    await r.agent_socket(socket, "dev")
    assert socket.closed and not r.connections
    assert not r.store.one("SELECT id FROM audit WHERE action='device.connected'")


@pytest.mark.asyncio
@pytest.mark.parametrize("hello", [
    {"type": "hello"},
    {"type": "hello", "delivery_protocol": 1, "journal_id": "legacy"},
    {"type": "hello", "delivery_protocol": 2},
    {"type": "hello", "delivery_protocol": 2, "journal_id": []},
])
async def test_agent_without_durable_journal_protocol_is_not_admitted(runtime, hello):
    r, _, secret = runtime
    socket = Socket(secret, hello=hello)
    await r.agent_socket(socket, "dev")
    assert not r.connections and socket.closed and not socket.packets


@pytest.mark.asyncio
async def test_disabled_device_is_fenced_before_close_completes(runtime):
    r, p, secret = runtime
    op = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    r.store.execute("UPDATE devices SET enabled=0 WHERE id='dev'")
    assert not r.online("dev")
    await r.deliver(op["operation_id"])
    assert not [packet for packet in socket.packets if packet["type"] == "call"]
    await r.disconnect_device("dev", "owner disabled")
    await reader
    assert not r.connections


@pytest.mark.asyncio
async def test_storage_failure_cannot_prevent_connection_teardown(runtime, monkeypatch):
    r, _, secret = runtime
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    def unavailable(*args, **kwargs):
        raise OSError("storage temporarily unavailable")
    monkeypatch.setattr(r.store, "execute", unavailable)
    await r.disconnect_device("dev", "owner disabled")
    await reader
    assert socket.closed and not r.connections


@pytest.mark.asyncio
async def test_authenticated_late_events_cannot_cross_key_rotation(runtime):
    r, p, secret = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    r.store.execute("UPDATE devices SET secret=? WHERE id='dev'", (r.store.encrypt(token()),))
    socket.incoming.put_nowait({"type": "result", "id": receipt["operation_id"], "result": {"ok": True, "data": {"content": "stale device"}}})
    await reader
    assert r.operation(receipt["operation_id"], p)["result"] is None


def mark_builtin(runtime, revision=0):
    runtime.store.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('builtin_agent', json.dumps({
        'version': 1, 'device_id': 'dev', 'owner_user_id': 'owner'})))
    runtime.store.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('builtin_agent_roots', json.dumps({
        'version': 1, 'revision': revision, 'roots': []})))


@pytest.mark.asyncio
async def test_builtin_identity_cannot_connect_over_websocket(runtime):
    r, _, secret = runtime
    mark_builtin(r)
    socket = Socket(secret)
    await r.agent_socket(socket, 'dev')
    assert socket.closed and not hasattr(socket, 'channel') and not r.connections


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['hello', 'ready'])
async def test_builtin_roots_changed_during_local_handshake_cannot_register(runtime, phase):
    r, _, secret = runtime
    mark_builtin(r)
    socket = Socket(secret, **{'before_hello' if phase == 'hello' else 'after_ready': lambda: mark_builtin(r, 1)})
    await r.agent_session(socket, 'dev', local_revision=0)
    assert socket.closed and not r.connections
    assert not r.store.one("SELECT id FROM audit WHERE action='device.connected'")


@pytest.mark.asyncio
async def test_builtin_root_revision_fences_dispatch_and_late_results(runtime):
    r, p, secret = runtime
    mark_builtin(r)
    receipt = await r.invoke('fs_read', {'project': 'Project', 'path': 'README.md'}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_session(socket, 'dev', local_revision=0))
    try:
        await socket.wait_for(socket.receiving.wait(), reader)
        assert r.online('dev') and r.connections['dev'].local_revision == 0
        mark_builtin(r, 1)
        assert not r.online('dev')
        await r.deliver(receipt['operation_id'])
        assert not [packet for packet in socket.packets if packet['type'] == 'call']
        socket.incoming.put_nowait({'type': 'result', 'id': receipt['operation_id'],
                                   'result': {'ok': True, 'data': {'content': 'stale roots'}}})
        await asyncio.wait_for(reader, 10)
        assert r.operation(receipt['operation_id'], p)['result'] is None
        assert not r.connections
    finally:
        await socket.close()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_external_identity_cannot_use_local_session(runtime):
    r, _, secret = runtime
    socket = Socket(secret)
    await r.agent_session(socket, 'dev', local_revision=0)
    assert socket.closed and not r.connections and not hasattr(socket, 'channel')


@pytest.mark.asyncio
async def test_reconnect_outbox_cannot_bypass_journal_epoch_fence(runtime):
    r, p, secret = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    r.store.execute("UPDATE operations SET attempts=1,journal_id='lost-journal' WHERE id=?", (receipt["operation_id"],))
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    socket.incoming.put_nowait({"type": "result", "id": receipt["operation_id"], "result": {"ok": True, "data": {"content": "foreign journal"}}})
    await until(lambda: any(item["type"] == "ack" for item in socket.packets))
    final = r.operation(receipt["operation_id"], p)
    assert final["state"] == "needs_review" and final["result"]["error"]["code"] == "JOURNAL_CHANGED"
    await socket.close()
    await reader


@pytest.mark.asyncio
async def test_final_result_is_immutable_even_with_stale_operation_snapshot(runtime):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    row = r.store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],))
    r.complete(row, {"ok": True, "data": {"content": "first result"}})
    r.complete(row, {"ok": True, "data": {"content": "duplicate result"}})
    assert r.operation(row["id"], p)["result"]["data"]["content"] == "first result"
    assert r.store.one("SELECT count(*) AS n FROM audit WHERE status='succeeded'")["n"] == 1


@pytest.mark.asyncio
async def test_completed_result_future_is_not_retained_by_expiry_timer(runtime):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    pending = weakref.ref(r.futures[receipt["operation_id"]])
    r.complete(r.store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],)), {"ok": True, "data": {"content": "large result"}})
    await asyncio.sleep(0)
    gc.collect()
    assert pending() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"ok": True, "data": None}, {"ok": False, "error": None}])
async def test_null_result_fields_remain_retrievable_on_idempotent_retry(runtime, result):
    r, p, _ = runtime
    args = {"project": "Project", "path": "README.md", "idempotency_key": "null-result-replay"}
    receipt = await r.invoke("fs_read", args, p)
    r.complete(r.store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],)), result)
    if result["ok"]:
        replayed=await r.invoke('fs_read',args,p)
        assert set(replayed)=={'operation_id','route'}
        assert replayed['operation_id']==receipt['operation_id']
        assert replayed['route']['device_id']=='dev' and replayed['route']['family']=='files'
        assert replayed['route']['source_key']=='device:dev'
        assert r.store.one('SELECT attempts FROM operations WHERE id=?',(receipt['operation_id'],))['attempts']==0
    else:
        with pytest.raises(DevError) as error:
            await r.invoke("fs_read", args, p)
        assert error.value.code == "REMOTE_ERROR"


@pytest.mark.asyncio
async def test_replayed_interrupted_result_is_acknowledged_without_reauditing(runtime):
    r, p, secret = runtime
    receipt = await r.invoke("tasks_run", {"project": "Project", "task": "test", "idempotency_key": "interrupted-replay"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    await until(lambda: r.online("dev"))
    packet = {"type": "result", "id": receipt["operation_id"], "result": {"ok": False, "error": {"code": "INTERRUPTED", "message": "local restart"}}}
    socket.incoming.put_nowait(packet)
    socket.incoming.put_nowait(packet)
    await until(lambda: len([item for item in socket.packets if item["type"] == "ack"]) == 2)
    await socket.close()
    await reader
    assert r.operation(receipt["operation_id"], p)["state"] == "needs_review"
    assert r.store.one("SELECT count(*) AS n FROM audit WHERE status='needs_review'")["n"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, [], {"ok": True, "data": ["bad"]}, {"ok": False, "error": {"message": {"bad": "type"}}}])
async def test_malformed_result_does_not_poison_agent_outbox(runtime, result):
    r, p, secret = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    socket = Socket(secret)
    reader = asyncio.create_task(r.agent_socket(socket, "dev"))
    try:
        # online() becomes true before registration finishes; receive_text's
        # second call proves the authenticated receive loop is ready.
        await socket.wait_for(socket.receiving.wait(), reader)
        assert r.online("dev")
        socket.incoming.put_nowait({"type": "result", "id": receipt["operation_id"], "result": result})
        assert await socket.wait_for(socket.acknowledgements.get(), reader) == {
            "type": "ack", "id": receipt["operation_id"]}
        assert r.online("dev")
        assert r.operation(receipt["operation_id"], p)["state"] == "needs_review"
        assert r.operation(receipt["operation_id"], p)["result"]["error"]["code"] == "INVALID_AGENT_RESULT"
    finally:
        await socket.close()
        await asyncio.wait_for(reader, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"tool": "fs_read", "args": {}, "project": []}, {"tool": "removed-tool", "args": {}, "project": {"root": "/tmp/project"}}])
async def test_invalid_durable_request_does_not_retry_forever(runtime, payload):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    r.store.execute("UPDATE operations SET payload=? WHERE id=?", (r.store.encrypt(json.dumps(payload)), receipt["operation_id"]))
    await r.deliver(receipt["operation_id"])
    assert r.operation(receipt["operation_id"], p)["state"] == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["reconnecting", "cancelling"])
async def test_missing_durable_payload_is_not_guessed_or_replayed_on_start(runtime, state):
    r, p, _ = runtime
    args = {"project": "Project", "path": "README.md", "idempotency_key": "corrupted-receipt"}
    receipt = await r.invoke("fs_read", args, p)
    r.store.execute("UPDATE operations SET state=?,payload=NULL WHERE id=?", (state, receipt["operation_id"]))
    await r.start()
    try:
        repeated = await r.invoke("fs_read", args, p)
        assert repeated["state"] == "needs_review" and not repeated["pending"] and repeated["next"] is None
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_scheduler_survives_temporary_storage_and_audit_failure(runtime, monkeypatch):
    r, p, _ = runtime
    receipt = await r.invoke("fs_read", {"project": "Project", "path": "README.md"}, p)
    original_all, original_audit = r.store.all, r.store.audit
    failed = asyncio.Event()
    def unavailable(*args, **kwargs):
        failed.set()
        raise OSError("injected storage unavailable")
    monkeypatch.setattr(r.store, "all", unavailable)
    monkeypatch.setattr(r.store, "audit", unavailable)
    r.worker = asyncio.create_task(r.delivery_loop())
    await failed.wait()
    await asyncio.sleep(0)
    assert not r.worker.done()
    monkeypatch.setattr(r.store, "all", original_all)
    monkeypatch.setattr(r.store, "audit", original_audit)
    r.store.execute("UPDATE operations SET deadline=1 WHERE id=?", (receipt["operation_id"],))
    r.wake.set()
    await until(lambda: not r.operation(receipt["operation_id"], p)["pending"])
    await r.stop()
