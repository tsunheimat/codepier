"""Portable contracts for Windows Session IO, not claims of native acceptance."""
from __future__ import annotations

import asyncio
import base64
import json
import threading
from types import SimpleNamespace

import pytest

from agent import session_windows
from agent.session_powershell import BOOTSTRAP, argv, command
from agent.session_windows import ConPTYProcess, native_call
from shared.util import DevError


class Native:
    pid = 42

    def __init__(self, count=None):
        self.count = count
        self.writes = []
        self.dimensions = None

    def write(self, text):
        self.writes.append(text)
        return len(text.encode('utf-8')) if self.count is None else self.count

    def read(self, blocking=False):
        assert blocking is False
        return '中文\x1b[0m'

    def set_size(self, cols, rows):
        self.dimensions = cols, rows


class Guard:
    def __init__(self):
        self.exit = None
        self.closed = False
        self.terminated = 0

    def exit_code(self):
        return self.exit

    def terminate(self):
        self.terminated += 1
        self.exit = 1

    def close(self):
        self.closed = True


def test_powershell_program_is_fixed_and_commands_are_data():
    values = argv('C:/Program Files/PowerShell/pwsh.exe')
    assert values[:4] == ['C:/Program Files/PowerShell/pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive']
    assert values[4] == '-EncodedCommand'
    assert base64.b64decode(values[5]).decode('utf-16le') == BOOTSTRAP
    text = "'quotes';\n$state='中文'; Write-Output 'CP1 fake 0 0 0'"
    encoded = command('a'*32, text)
    assert encoded.count(b'\n') == 1
    payload = json.loads(encoded)
    assert payload['id'] == 'a'*32
    assert base64.b64decode(payload['command']).decode('utf-8') == text
    assert text not in BOOTSTRAP
    assert '. ([ScriptBlock]::Create($__cp_command))' in BOOTSTRAP
    assert '3>&2' not in BOOTSTRAP  # PowerShell permits stream merging to 1 only.


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['ascii\r', '中文\r', '\x03', '🙂'])
async def test_conpty_reports_original_utf8_write_length(text):
    native, guard = Native(), Guard()
    process = ConPTYProcess(native, guard)
    assert await process.write(text.encode()) == len(text.encode())
    assert native.writes == [text]
    assert process.read() == '中文\x1b[0m'.encode()
    process.resize(31, 99)
    assert native.dimensions == (99, 31)
    assert process.returncode is None
    process.terminate()
    assert await process.wait() == 1
    process.release()
    process.release()
    assert guard.closed and guard.terminated == 1
    assert process.returncode == 1


@pytest.mark.asyncio
async def test_partial_conpty_write_is_not_replayed():
    native = Native(count=2)
    process = ConPTYProcess(native, Guard())
    assert await process.write(b'four') == 2
    assert native.writes == ['four']


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'\xff', b'\xf0\x9f', b'\xe4'])
async def test_invalid_or_split_utf8_sends_no_terminal_input(raw):
    native = Native()
    with pytest.raises(DevError) as failure:
        await ConPTYProcess(native, Guard()).write(raw)
    assert failure.value.code == 'PTY_ENCODING_UNSUPPORTED'
    assert native.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [-1, 100, None, '1'])
async def test_invalid_native_receipt_does_not_retry(count):
    native = Native()
    def unconfirmed(text):
        native.writes.append(text)
        return count
    native.write = unconfirmed
    with pytest.raises(OSError):
        await ConPTYProcess(native, Guard()).write(b'x')
    assert native.writes == ['x']


@pytest.mark.asyncio
async def test_blocked_native_write_does_not_block_loop_or_replay_after_cancel():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def blocked(text):
        calls.append(text)
        entered.set()
        try:
            release.wait(5)
            return len(text)
        finally:
            finished.set()
    task = asyncio.create_task(native_call(blocked, 'input'))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        assert entered.is_set() and not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(.01)
        assert calls == ['input']
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 3)


def test_missing_conpty_does_not_fall_back_to_pipe(monkeypatch, tmp_path):
    monkeypatch.setattr(session_windows, 'capabilities', lambda: {'available': False, 'reason': 'missing fixture'})
    with pytest.raises(DevError) as failure:
        ConPTYProcess.start(['cmd.exe'], tmp_path, {}, 24, 80)
    assert failure.value.code == 'PTY_PLATFORM_UNSUPPORTED'


def test_conpty_start_uses_exact_runner_plan_and_owned_job(monkeypatch, tmp_path):
    native, guard, calls = Native(), Guard(), []
    monkeypatch.setattr(session_windows, 'capabilities', lambda: {'available': True})
    def create(arguments, cwd, env, rows, cols):
        calls.append((arguments, cwd, env, rows, cols))
        return native
    monkeypatch.setattr(session_windows, 'create_pty', create)
    monkeypatch.setattr(session_windows, 'ProcessJob', lambda pid: guard if pid == 42 else None)
    process = ConPTYProcess.start(['ssh.exe', '-tt', 'saved-host'], tmp_path, {'PATH': 'fixture'}, 30, 120)
    assert calls == [(['ssh.exe', '-tt', 'saved-host'], str(tmp_path), {'PATH': 'fixture'}, 30, 120)]
    assert process.guard is guard
    process.terminate()
    process.release()


def test_windows_local_default_and_cmd_mode_are_explicit(tmp_path, monkeypatch):
    from agent import execution
    from tests.test_execution_workspace import setup
    agent, project = setup(tmp_path)
    monkeypatch.setattr(execution, 'os', SimpleNamespace(name='nt', environ={'PATH': 'fixture'}))
    monkeypatch.setattr(execution.shutil, 'which', lambda name, **kw: 'C:/bin/' + name + '.exe')
    plan = execution.LocalBackend(agent).plan(project, {}, session=True)
    assert plan.shell == 'powershell' and '-EncodedCommand' in plan.argv
    tty = execution.LocalBackend(agent).plan(project, {'shell': 'cmd'}, session=True, tty=True)
    assert tty.argv[-3:] == ['/D', '/Q', '/K']
    with pytest.raises(DevError, match='interactive Windows PTY'):
        execution.LocalBackend(agent).plan(project, {'shell': 'cmd'}, session=True)
