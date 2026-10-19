"""Agent local one-shot session, startup cleanup, and CLI admission regressions."""
from __future__ import annotations

from shared.tool_protocol import advertisement, catalog_digest

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent.runner import Agent
from shared.crypto import SecureChannel, token
from shared.instance_lock import InstanceLock
from shared.local_transport import PipeTransport
from shared.util import atomic_json

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def local_agent(tmp_path):
    path = tmp_path / "config.json"
    atomic_json(path, {"hub_url": "http://127.0.0.1:9", "device_id": "fixture", "secret": token(),
                       "allowed_roots": [], "state_dir": str(tmp_path / "state"), "tasks": {}})
    agent = Agent(path)
    yield agent
    agent.journal.db.close()
    agent.instance_lock.close()


class Socket:
    def __init__(self, agent, *, reject=False):
        self.challenge = token()
        self.channel = SecureChannel(agent.config["secret"], self.challenge, "fixture", "hub")
        self.receives = 0
        self.hello = None
        self.closed = False
        self.reject = reject

    async def recv(self):
        self.receives += 1
        if self.receives == 1:
            return json.dumps({"type": "challenge", "challenge": self.challenge})
        return self.channel.pack({"type": "wrong" if self.reject else "ready", **advertisement(catalog_digest())})

    async def send(self, packet):
        data = self.channel.unpack(packet)
        if data["type"] == "hello":
            self.hello = data

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ConnectionError("parent EOF")

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_local_run_handshakes_once_never_dials_or_watches_config(local_agent, monkeypatch, reject):
    agent = local_agent
    socket = Socket(agent, reject=reject)

    def forbidden(*args, **kwargs):
        raise AssertionError("local mode must not reconnect, dial WS, or hot reload")

    monkeypatch.setattr("agent.runner.websockets.connect", forbidden)
    monkeypatch.setattr(agent, "watch_config", forbidden)
    monkeypatch.setattr("agent.brand_upgrade.watch", forbidden)
    await asyncio.wait_for(agent.run(local_transport=socket), 3)
    assert socket.receives == 2 and socket.hello["type"] == "hello"
    assert socket.hello["journal_id"]
    assert agent.connection_number == (0 if reject else 1)
    assert socket.closed and agent.stop_event.is_set()
    assert agent.instance_lock.file.closed
    assert agent.socket is None and agent.channel is None
    with InstanceLock(agent.state_dir / ".agent.lock"):
        pass


@pytest.mark.asyncio
async def test_remote_connection_keeps_config_snapshot_while_opening(local_agent, monkeypatch):
    agent = local_agent
    original = agent.config
    socket = Socket(agent)

    class Opening:
        async def __aenter__(self):
            # A hot reload while the network connection opens must not exchange
            # the new Hub's credentials with the previous configured endpoint.
            agent.config = {**original, 'secret': token(), 'hub_url': 'http://127.0.0.1:10'}
            return socket

        async def __aexit__(self, *args):
            await socket.close()

    monkeypatch.setattr('agent.runner.websockets.connect', lambda *args, **kwargs: Opening())
    await agent.connect_once()
    assert socket.hello is not None and socket.closed
    assert agent.connection_number == 0 and agent.socket is None


@pytest.mark.asyncio
async def test_stop_during_local_handshake_closes_transport_and_lock(local_agent):
    agent = local_agent
    entered = asyncio.Event()
    socket = Socket(agent)

    async def receive():
        entered.set()
        await asyncio.Event().wait()

    socket.recv = receive
    running = asyncio.create_task(agent.run(local_transport=socket))
    await entered.wait()
    await agent.stop()
    await asyncio.wait_for(running, 2)
    assert socket.closed and agent.instance_lock.file.closed


@pytest.mark.asyncio
async def test_local_startup_failure_still_closes_transport_and_lock(local_agent, monkeypatch):
    socket = Socket(local_agent)

    async def fail():
        raise RuntimeError("startup failed")

    monkeypatch.setattr(local_agent.integrations, "start", fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        await local_agent.run(local_transport=socket)
    assert socket.closed and local_agent.instance_lock.file.closed


@pytest.mark.parametrize("args", [["--local-stdio"], ["--supervised", "--local-stdio", "--parent-stdin"]])
def test_local_cli_requires_supervision_and_excludes_stdin_guard(args):
    result = subprocess.run([sys.executable, "-m", "agent", "run", *args],
                            cwd=REPO, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert not result.stdout
    assert b"--local-stdio" in result.stderr


@pytest.mark.asyncio
async def test_real_cli_eof_releases_state_lock_without_ws_fallback(tmp_path):
    path = tmp_path / "config.json"
    secret = token()
    atomic_json(path, {"hub_url": "http://127.0.0.1:9", "device_id": "fixture", "secret": secret,
                       "allowed_roots": [], "state_dir": str(tmp_path / "state"), "tasks": {}})
    child = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent", "--config", str(path),
        "run", "--supervised", "--local-stdio", cwd=REPO, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    transport = PipeTransport(child.stdout, child.stdin)
    try:
        challenge = token()
        channel = SecureChannel(secret, challenge, "fixture", "hub")
        await transport.send_json({"type": "challenge", "challenge": challenge})
        hello = channel.unpack(await asyncio.wait_for(transport.recv(), 10))
        assert hello["type"] == "hello"
        await transport.send(channel.pack({"type": "ready", **advertisement(catalog_digest())}))
        packet = channel.unpack(await asyncio.wait_for(transport.recv(), 5))
        assert packet["type"] in {"heartbeat", "native_sessions"}
        await transport.close()
        await asyncio.wait_for(child.wait(), 10)
        assert child.returncode == 0, (await child.stderr.read()).decode()
        with InstanceLock(tmp_path / "state" / ".agent.lock"):
            pass
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()


@pytest.mark.asyncio
async def test_cli_parent_eof_during_hung_agent_initialization(tmp_path):
    code = '''
import os, sys, time
from pathlib import Path
import agent.runner
from agent.__main__ import main
from shared.instance_lock import InstanceLock
from shared.local_transport import StdioTransport
initialize = StdioTransport.__init__
def quick_guard(self, *args, **kwargs):
    initialize(self, *args, exit_timeout=.1, **kwargs)
StdioTransport.__init__ = quick_guard
class StuckAgent:
    def __init__(self, path):
        self.lock = InstanceLock(Path(sys.argv[2]).parent / "startup.lock")
        print("init diagnostic", flush=True)
        os.write(2, b"init-ready\\n")
        time.sleep(30)
agent.runner.Agent = StuckAgent
sys.argv = ["agent", "--config", sys.argv[1], "run", "--supervised", "--local-stdio"]
main()
'''
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(tmp_path / "config.json"),
        cwd=REPO, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(child.stderr.readline(),10)==b"init diagnostic"+os.linesep.encode()
        assert await asyncio.wait_for(child.stderr.readline(), 2) == b"init-ready\n"
        child.stdin.close()
        await asyncio.wait_for(child.wait(), 3)
        assert child.returncode == 0
        assert await child.stdout.read() == b""
        with InstanceLock(tmp_path / "startup.lock"):
            pass
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()
