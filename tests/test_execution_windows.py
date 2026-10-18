"""Real native Windows Session acceptance; run explicitly on a Windows CI worker.

Only disposable local processes and temporary working directories are used. This
is distinct from portable mocked ConPTY contract tests and never contacts a VPS.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import os
import shutil
from dataclasses import dataclass

import pytest

from agent.execution_sessions import SessionManager
from agent.session_conpty import capabilities
from shared.util import DevError
from tests.test_execution_workspace import setup, args

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Native Windows acceptance requires Windows')


@pytest.fixture(params=['powershell', 'pwsh'])
def native_shell(request, monkeypatch):
    original = shutil.which
    executable = original(request.param)
    assert executable, f'Acceptance worker must provide {request.param}'
    def resolve(name, **kwargs):
        if name == 'pwsh':
            return executable if request.param == 'pwsh' else None
        return original(name, **kwargs)
    monkeypatch.setattr('agent.execution.shutil.which', resolve)
    return request.param


@pytest.mark.asyncio
async def test_windows_persistent_shell_keeps_variables_functions_env_cwd(tmp_path, native_shell):
    agent, project = setup(tmp_path)
    (tmp_path/'sub').mkdir()
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        opened = await manager.execute(sid, project, args('open', project='demo'))
        assert opened['state'] == 'active' and opened['shell'] == 'powershell'
        first = await manager.execute('b'*32, project, args('exec', session_id=sid,
            command="$saved='中文🙂'; $env:CP_TEST='env-kept'; function SavedFunction { 'fn-kept' }; Set-Location sub"))
        assert first['exit_code'] == 0, first
        second = await manager.execute('c'*32, project, args('exec', session_id=sid,
            command="Write-Output $saved; Write-Output $env:CP_TEST; SavedFunction; (Get-Location).Path"))
        assert second['exit_code'] == 0, second
        for expected in ['中文🙂', 'env-kept', 'fn-kept', str(tmp_path/'sub')]:
            assert expected in second['stdout'], second
        assert base64.b64decode(second['stdout_base64']).decode() == second['stdout']
        assert (await manager.execute('d'*32, project, args('status', session_id=sid)))['state'] == 'active'
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_windows_persistent_output_is_framed_and_failures_do_not_replay(tmp_path, native_shell):
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        await manager.execute(sid, project, args('open', project='demo'))
        framed = await manager.execute('b'*32, project, args('exec', session_id=sid,
            command="Write-Output 'CP1 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 0 0 0'; Write-Host 'host-output'; Write-Warning 'warning-output'"))
        assert framed['exit_code'] == 0, framed
        assert 'CP1 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 0 0 0' in framed['stdout']
        assert 'host-output' in framed['stdout'] and 'warning-output' in framed['stdout']
        failure = await manager.execute('c'*32, project, args('exec', session_id=sid, command="throw 'fixture-error'"))
        assert failure['exit_code'] != 0 and 'fixture-error' in failure['stderr'], failure
        native = await manager.execute('d'*32, project, args('exec', session_id=sid, command='cmd.exe /d /c exit 7'))
        assert native['exit_code'] == 7, native
        okay = await manager.execute('e'*32, project, args('exec', session_id=sid, command="Write-Output 'still-original'"))
        assert okay['exit_code'] == 0 and 'still-original' in okay['stdout'], okay
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_windows_persistent_timeout_and_connection_ownership(tmp_path, native_shell):
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        await manager.execute(sid, project, args('open', project='demo'))
        with pytest.raises(DevError) as failure:
            await manager.execute('b'*32, {**project, '_integration_owner': 'grant:another'}, args('status', session_id=sid))
        assert failure.value.code == 'SESSION_LOST'
        result = await manager.execute('c'*32, project, args('exec', session_id=sid,
            command='Start-Sleep -Seconds 30', timeout_seconds=1))
        assert result['timed_out'] and result['state'] == 'lost' and result['outcome_uncertain'], result
        assert manager.handles[sid].process.returncode is not None
        assert manager.handles[sid].guard is None
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('shell', ['cmd', 'powershell'])
async def test_real_conpty_read_write_resize_and_original_close(tmp_path, shell):
    assert capabilities()['available'], capabilities()
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    try:
        opened = await manager.execute(sid, project, args('open', project='demo', mode='pty', shell=shell))
        assert opened['state'] == 'active' and opened['shell'] == shell
        resized = await manager.execute('b'*32, project, args('resize', session_id=sid, rows=39, cols=117))
        assert (resized['rows'], resized['cols']) == (39, 117)
        # Wait for the real console to initialize, rather than reading its prompt
        # as if that prompt were a per-command completion marker.
        for _ in range(200):
            if (await manager.execute('c'*32, project, args('read', session_id=sid)))['end_cursor']:
                break
            await asyncio.sleep(.05)
        assert manager.handles[sid].process.returncode is None
        text = 'echo cp' + '-terminal-output>pty-proof.txt\r'
        receipt = await manager.execute('d'*32, project, args('write', session_id=sid, data=text))
        assert receipt['bytes_written'] == len(text.encode()) and not receipt.get('outcome_uncertain'), receipt
        output = ''
        cursor = 0
        for _ in range(200):
            chunk = await manager.execute('e'*32, project, args('read', session_id=sid, cursor=cursor))
            assert chunk['lost_bytes'] == 0
            cursor = chunk['cursor']
            output += base64.b64decode(chunk['data_base64']).decode('utf-8', 'replace')
            if 'cp-terminal-output' in output:
                break
            await asyncio.sleep(.05)
        assert 'cp-terminal-output' in output, output
        for _ in range(200):
            if (tmp_path/'pty-proof.txt').exists():
                break
            await asyncio.sleep(.05)
        raw = (tmp_path/'pty-proof.txt').read_bytes()
        encoding = 'utf-16' if raw.startswith(b'\xff\xfe') else 'utf-8-sig'
        assert raw.decode(encoding).strip() == 'cp-terminal-output'
        with pytest.raises(DevError, match='complete UTF-8'):
            await manager.execute('f'*32, project, args('write', session_id=sid, data_base64='/w=='))
        closed = await manager.execute('1'*32, project, args('close', session_id=sid))
        assert closed['state'] == 'closed'
        assert manager.handles[sid].process.returncode is not None
        assert manager.handles[sid].process.guard is None
        assert (await manager.execute('2'*32, project, args('close', session_id=sid)))['state'] == 'closed'
    finally:
        await manager.close()


@dataclass
class ChildHandle:
    value: int

    @classmethod
    def open(cls, pid):
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        value = kernel.OpenProcess(0x00100000, False, pid)
        assert value, ctypes.WinError(ctypes.get_last_error())
        return cls(value)

    def wait(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        return kernel.WaitForSingleObject(self.value, 5000)

    def close(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(self.value)


@pytest.mark.asyncio
async def test_windows_close_terminates_owned_child_tree(tmp_path, native_shell):
    agent, project = setup(tmp_path)
    manager = SessionManager(agent)
    sid = 'a'*32
    child = None
    try:
        await manager.execute(sid, project, args('open', project='demo'))
        result = await manager.execute('b'*32, project, args('exec', session_id=sid,
            command="(Start-Process cmd.exe -ArgumentList '/d /c ping -n 120 127.0.0.1' -PassThru -WindowStyle Hidden).Id"))
        assert result['exit_code'] == 0, result
        child = ChildHandle.open(int(result['stdout'].strip()))
        await manager.execute('c'*32, project, args('close', session_id=sid))
        assert await asyncio.to_thread(child.wait) == 0
    finally:
        await manager.close()
        if child is not None:
            child.close()
