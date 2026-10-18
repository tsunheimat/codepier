"""Real local Shell/PTY processes and saved SSH argument/authority contracts."""
import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.execution_sessions import OutputBuffer, SessionManager, validate_sessions
from agent.execution import SSHBackend
from agent.shell import validate_shell
from agent.ssh import validate_ssh
from shared.session_contracts import Session
from shared.util import DevError


class Engine:
    def __init__(self, root): self.base = root
    def root(self, project, write=False):
        assert project['root'] == str(self.base)
        return self.base, {'writable': True, 'allow_tasks': True}
    def path(self, root, relative):
        p = (root / relative).resolve()
        if not p.is_relative_to(root): raise DevError('PATH_OUTSIDE', 'outside')
        return p


def setup(tmp_path):
    config = {'shell': validate_shell({'enabled': True, 'projects': ['*']}),
              'sessions': validate_sessions({'enabled': True}), 'mcp_policy': {},
              'ssh': validate_ssh({'enabled': True, 'targets': {'prod': {'host_alias': 'production'}}})}
    agent = SimpleNamespace(config=config, engine=Engine(tmp_path), state_dir=tmp_path)
    project = {'id': 'project', 'alias': 'demo', 'device_id': 'runner', 'root': str(tmp_path),
               'mode': 'write', 'allow_tasks': True, '_integration_owner': 'grant:one'}
    return agent, project


def args(operation, **kw):
    return Session(operation=operation, idempotency_key='test-intent-0001', **kw).model_dump()


def test_buffer_is_bytes_and_reports_loss():
    buffer = OutputBuffer(4)
    buffer.append(b'abc\xffde')
    result = buffer.read(0, 3)
    assert result['lost_bytes'] == 2 and result['cursor'] == 5
    assert base64.b64decode(result['data_base64']) == b'c\xffd'
    with pytest.raises(DevError): buffer.read(99, 5)


@pytest.mark.asyncio
async def test_persistent_commands_keep_state_and_frame_arbitrary_output(tmp_path):
    agent, project = setup(tmp_path)
    (tmp_path/'sub').mkdir()
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        result = await manager.execute(sid, project, args('open', project='demo'))
        assert result['state'] == 'active'
        first = await manager.execute('b'*32, project, args('exec', session_id=sid,
            command="cd sub && export HELLO='hello world'; printf 'CP1 fake 0 0 0\\n'; printf 'oops' >&2"))
        assert first['stdout'] == 'CP1 fake 0 0 0\n' and first['stderr'] == 'oops'
        second = await manager.execute('c'*32, project, args('exec', session_id=sid, command='printf "%s\\n%s" "$HELLO" "$PWD"'))
        assert second['exit_code'] == 0
        assert second['stdout'] == 'hello world\n'+str(tmp_path/'sub')
        third = await manager.execute('d'*32, project, args('exec', session_id=sid, command='false'))
        assert third['exit_code'] == 1
        fourth = await manager.execute('e'*32, project, args('exec', session_id=sid, command='printf still-alive'))
        assert fourth['stdout'] == 'still-alive'
        with pytest.raises(DevError):
            await manager.execute('f'*32, {**project, '_integration_owner': 'grant:other'}, args('status', session_id=sid))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_lost_shell_is_not_reopened_and_timeout_is_uncertain(tmp_path):
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        await manager.execute(sid, project, args('open', project='demo'))
        result = await manager.execute('b'*32, project, args('exec', session_id=sid, command='sleep 10', timeout_seconds=1))
        assert result['state'] == 'lost' and result['timed_out'] and result['outcome_uncertain']
        with pytest.raises(DevError):
            await manager.execute('c'*32, project, args('exec', session_id=sid, command='touch should-not-exist'))
        assert not (tmp_path/'should-not-exist').exists()
        replacement = SessionManager(agent)
        with pytest.raises(DevError):
            await replacement.execute('d'*32, project, args('status', session_id=sid))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_real_pty_read_write_resize_and_cleanup(tmp_path):
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        opened = await manager.execute(sid, project, args('open', project='demo', mode='pty'))
        assert opened['state'] == 'active'
        write = await manager.execute('b'*32, project, args('write', session_id=sid,
            data_base64=base64.b64encode(b'printf "PTY_%s\\n" SUCCESS\n').decode()))
        assert write['bytes_written'] > 0
        for _ in range(100):
            result = await manager.execute('c'*32, project, args('read', session_id=sid))
            if 'PTY_SUCCESS' in result['data']: break
            await asyncio.sleep(.02)
        assert 'PTY_SUCCESS' in result['data']
        resized = await manager.execute('d'*32, project, args('resize', session_id=sid, rows=40, cols=120))
        assert resized['rows'] == 40
        closed = await manager.execute('e'*32, project, args('close', session_id=sid))
        assert closed['state'] == 'closed'
        assert manager.handles[sid].master is None
    finally:
        await manager.close()


def test_ssh_uses_saved_alias_and_no_local_project_root(tmp_path, monkeypatch):
    agent, _ = setup(tmp_path)
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **k: '/usr/bin/ssh')
    project = {'id': None, 'root': '', '_vps_context': {'id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'},
               '_ssh_target': {'vps_id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'}}
    spec = SSHBackend(agent).plan(project, {'command': 'printf ok', 'cwd': '/srv/a folder', 'env': {'X': "a'b"}, 'timeout_seconds': 30})
    assert spec.remote and spec.cwd == tmp_path
    assert spec.argv[-2] == 'production' and 'BatchMode=yes' in spec.argv
    assert 'sshpass' not in spec.argv and 'StrictHostKeyChecking=yes' in spec.argv
    import shlex
    script = shlex.split(spec.argv[-1])[2]
    assert "cd -- '/srv/a folder'" in script and 'printf ok' in script
    with pytest.raises(DevError):
        SSHBackend(agent).plan({**project, '_ssh_target': {**project['_ssh_target'], 'connection_ref': 'attacker'}}, {'command': 'true'})


@pytest.mark.parametrize('target', [{'host_alias': '-oProxyCommand=bad'}, {'host_alias': 'valid', 'password': 'secret'},
                                  {'host_alias': 'valid', 'config_file': 'relative'}])
def test_rejects_unreviewed_or_unsafe_ssh_configuration(target):
    with pytest.raises(ValueError): validate_ssh({'targets': {'ref': target}})


@pytest.mark.parametrize('shell', ['sh', 'bash', 'zsh'])
def test_ssh_session_uses_saved_shell_without_client_discovery(tmp_path, monkeypatch, shell):
    agent, _ = setup(tmp_path)
    agent.config['ssh']['targets']['prod']['shell'] = shell
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **k: '/usr/bin/ssh')
    project = {'id': None, 'root': '',
        '_vps_context': {'id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'},
        '_ssh_target': {'vps_id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'}}
    request = args('open', target='vps:' + 'a'*32)
    spec = SSHBackend(agent).plan(project, request, session=True)
    assert spec.shell == shell
    assert spec.argv[-1].startswith(shell + ' -c ')


def test_ssh_session_open_does_not_consume_a_command_timeout(tmp_path, monkeypatch):
    agent, _ = setup(tmp_path)
    agent.config['ssh']['max_timeout_seconds'] = 30
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **k: '/usr/bin/ssh')
    project = {'id': None, 'root': '',
        '_vps_context': {'id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'},
        '_ssh_target': {'vps_id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'}}
    spec = SSHBackend(agent).plan(project, args('open', target='vps:' + 'a'*32), session=True)
    assert spec.remote
    # One-shot command timeouts still obey the Runner's configured ceiling.
    with pytest.raises(DevError) as failure:
        SSHBackend(agent).plan(project, {'command': 'true', 'timeout_seconds': 31})
    assert failure.value.code == 'SSH_TIMEOUT_LIMIT'


def test_ssh_session_explicit_shell_cannot_override_saved_target(tmp_path, monkeypatch):
    agent, _ = setup(tmp_path)
    agent.config['ssh']['targets']['prod']['shell'] = 'bash'
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **k: '/usr/bin/ssh')
    project = {'id': None, 'root': '',
        '_vps_context': {'id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'},
        '_ssh_target': {'vps_id': 'a'*32, 'resource_id': 'resource', 'connection_ref': 'prod'}}
    with pytest.raises(DevError) as failure:
        SSHBackend(agent).plan(project, args('open', target='vps:' + 'a'*32, shell='zsh'), session=True)
    assert failure.value.code == 'SSH_SHELL_MISMATCH'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['persistent_shell', 'pty'])
async def test_live_session_policy_restricts_input_but_preserves_cleanup(tmp_path, mode):
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        await manager.execute(sid, project, args('open', project='demo', mode=mode))
        agent.config['mcp_policy']['block_local_codex'] = True
        operation = 'exec' if mode == 'persistent_shell' else 'write'
        payload = {'command': 'printf forbidden'} if operation == 'exec' else {'data': 'printf forbidden\n'}
        with pytest.raises(DevError) as failure:
            await manager.execute('b'*32, project, args(operation, session_id=sid, **payload))
        assert failure.value.code == 'SESSION_POLICY_UNSUPPORTED'
        # Tightened input policy cannot strand the original process: reading its
        # status/output and closing it remain possible without sending new input.
        assert (await manager.execute('c'*32, project, args('status', session_id=sid)))['state'] == 'active'
        await manager.execute('d'*32, project, args('read', session_id=sid))
        assert (await manager.execute('e'*32, project, args('close', session_id=sid)))['state'] == 'closed'
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['persistent_shell', 'pty'])
async def test_queued_session_input_rechecks_policy_after_lock(tmp_path, mode):
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    pending = None
    try:
        await manager.execute(sid, project, args('open', project='demo', mode=mode))
        lock = manager.handles[sid].lock
        await lock.acquire()
        operation = 'exec' if mode == 'persistent_shell' else 'write'
        payload = {'command': 'printf queued'} if operation == 'exec' else {'data': 'printf queued\n'}
        pending = asyncio.create_task(manager.execute('b'*32, project,
            args(operation, session_id=sid, **payload)))
        await asyncio.sleep(0)
        assert not pending.done()
        agent.config['mcp_policy']['block_local_codex'] = True
        lock.release()
        with pytest.raises(DevError) as failure:
            await pending
        assert failure.value.code == 'SESSION_POLICY_UNSUPPORTED'
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await manager.close()
