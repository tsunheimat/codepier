"""Runner-owned live shell/PTY handles; the durable Journal owns operation retries.

Persistent commands redirect their output into per-command files. Only a framed
control stream is read by the manager, never a shell prompt or a user stdout
sentinel. Losing a process never causes implicit re-creation of shell state.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import struct
import sys
import time

from agent.execution import LocalBackend, SSHBackend
from agent.ssh import target_fingerprint
from agent.session_windows import ConPTYProcess
from shared.execution_policy import agent_blocks_codex, enforce_argv
from shared.util import DevError

OUTPUT_LIMIT = 1024 * 1024


class OutputBuffer:
    def __init__(self, capacity=256 * 1024):
        self.capacity, self.end = capacity, 0
        self.data = bytearray()

    def append(self, data):
        self.end += len(data)
        self.data.extend(data)
        if len(self.data) > self.capacity:
            del self.data[:-self.capacity]

    def read(self, cursor, limit):
        if cursor > self.end:
            raise DevError('CURSOR_AHEAD', 'Cursor is beyond this original Session output')
        start = self.end - len(self.data)
        actual = max(start, cursor)
        chunk = bytes(self.data[actual - start:actual - start + limit])
        return {'data': chunk.decode('utf-8', errors='replace'),
                'data_base64': base64.b64encode(chunk).decode('ascii'),
                'cursor': actual + len(chunk), 'lost_bytes': max(0, start - cursor), 'end_cursor': self.end}


def validate_sessions(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'max_sessions', 'idle_seconds', 'lifetime_seconds'}:
        raise ValueError('sessions contains unknown settings')
    result = {'enabled': False, 'max_sessions': 16, 'idle_seconds': 1800, 'lifetime_seconds': 86400, **value}
    if type(result['enabled']) is not bool:
        raise ValueError('sessions.enabled must be boolean')
    for key, lower, upper in [('max_sessions', 1, 100), ('idle_seconds', 30, 86400), ('lifetime_seconds', 30, 604800)]:
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise ValueError('Invalid sessions.' + key)
    return result


def identity(project):
    owner = project.get('_integration_owner') or project.get('_coding_owner')
    if not owner:
        raise DevError('SESSION_IDENTITY_REQUIRED', 'A Hub-authenticated connection identity is required', 403)
    fields = {key: project.get(key) for key in ('id', 'device_id', '_vps_context', '_route_context', '_source_boundary')}
    return hashlib.sha256(json.dumps([owner, fields], sort_keys=True).encode()).hexdigest()


@dataclass
class Handle:
    id: str
    binding: str
    mode: str
    process: asyncio.subprocess.Process | ConPTYProcess
    remote: bool
    config_hash: str | None
    shell: str
    state: str = 'active'
    born: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    output: OutputBuffer = field(default_factory=OutputBuffer)
    errors: OutputBuffer = field(default_factory=lambda: OutputBuffer(32768))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader: asyncio.Task | None = None
    master: int | None = None
    guard: object | None = None
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False
    limits: dict = field(default_factory=lambda: validate_sessions({}))

    def status(self):
        if self.state == 'active' and self.process.returncode is not None:
            self.state = 'lost'
        return {'session_id': self.id, 'mode': self.mode, 'shell': self.shell, 'state': self.state,
                'exit_code': self.process.returncode, 'outcome_uncertain': self.remote and self.state in {'lost', 'expired', 'closed'},
                'end_cursor': self.output.end}


class SessionManager:
    def __init__(self, agent):
        self.agent = agent
        self.handles: dict[str, Handle] = {}
        self.reaper: asyncio.Task | None = None
        self.open_lock = asyncio.Lock()
        self.executing: dict[str, Handle] = {}

    def cancel_operation(self, identifier):
        handle=self.executing.get(identifier)
        if handle is None:
            return None
        # Closing the retained process releases its current command read without
        # creating a replacement process or replaying any input.
        return asyncio.create_task(self._close(handle,'lost'))

    def cancelled(self, identifier):
        return identifier in getattr(self.agent,'cancelled',set())

    def active(self):
        return [handle for handle in self.handles.values() if handle.status()['state'] == 'active']

    async def _stderr(self, handle):
        while data := await handle.process.stderr.read(32768):
            handle.errors.append(data)

    async def _pty_output(self, handle):
        loop = asyncio.get_running_loop()
        while handle.master is not None:
            ready = loop.create_future()
            fd = handle.master
            def readable():
                if not ready.done():
                    ready.set_result(None)
            loop.add_reader(fd, readable)
            try:
                await ready
                try:
                    data = os.read(fd, 32768)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                handle.output.append(data)
            finally:
                loop.remove_reader(fd)
        await handle.process.wait()
        await self._close(handle, 'lost')

    async def _windows_pty_output(self, handle):
        drained_at = None
        try:
            while handle.state == 'active':
                data = await asyncio.to_thread(handle.process.read)
                if data:
                    handle.output.append(data)
                    drained_at = None
                elif handle.process.returncode is not None:
                    # ConPTY can retain an open output pipe after child exit.
                    # Drain late output without waiting forever for a false EOF.
                    drained_at = drained_at or time.monotonic()
                    if time.monotonic() - drained_at >= 0.25:
                        break
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception:
            handle.errors.append(b'ConPTY output transport ended unexpectedly')
        await self._close(handle, 'lost')

    async def _reap(self):
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for handle in list(self.handles.values()):
                limits = handle.limits
                if handle.state == 'active' and (now - handle.born > limits['lifetime_seconds'] or
                        not handle.busy and now - handle.touched > limits['idle_seconds']):
                    await self._close(handle, 'expired')
            # Closed handles can be forgotten: the Journal/Hub retain receipts.
            # Subsequent calls report LOST rather than ever reopening the shell.
            for identifier, handle in list(self.handles.items()):
                if handle.state != 'active' and now - handle.touched > 600:
                    await self._close(handle, handle.state)
                    self.handles.pop(identifier, None)

    async def _close(self, handle, state='closed'):
        async with handle.close_lock:
            # A command's EOF cannot overwrite explicit close/reaper expiry.
            if handle.state == 'active':
                handle.state = state
            try:
                if handle.guard is not None:
                    handle.guard.terminate()
                elif isinstance(handle.process, ConPTYProcess):
                    handle.process.terminate()
                elif handle.process.returncode is None:
                    if os.name == 'posix':
                        os.killpg(handle.process.pid, signal.SIGTERM)
                    else:
                        handle.process.terminate()
                if handle.process.returncode is None:
                    try:
                        await asyncio.wait_for(handle.process.wait(), 2)
                    except asyncio.TimeoutError:
                        if handle.guard is not None:
                            handle.guard.terminate()
                        elif os.name == 'posix':
                            os.killpg(handle.process.pid, signal.SIGKILL)
                        else:
                            handle.process.kill()
                        await handle.process.wait()
            except ProcessLookupError:
                pass
            if handle.reader and handle.reader is not asyncio.current_task():
                handle.reader.cancel()
                await asyncio.gather(handle.reader, return_exceptions=True)
            if handle.master is not None:
                os.close(handle.master)
                handle.master = None
            if handle.process.stdin:
                handle.process.stdin.close()
            if handle.guard is not None:
                handle.guard.close()
                handle.guard = None
            if isinstance(handle.process, ConPTYProcess):
                handle.process.release()

    def _local_plan(self, project, args, tty):
        return LocalBackend(self.agent).plan(project, args, session=True, tty=tty)

    async def open(self, identifier, project, args):
        settings = self.agent.config.get('sessions') or validate_sessions({})
        if not settings['enabled']:
            raise DevError('SESSIONS_DISABLED', 'Enable sessions in this Runner’s local configuration', 403)
        # A static command invocation guard cannot constrain arbitrary future PTY
        # keystrokes or stateful eval; refuse rather than implying such a sandbox.
        if agent_blocks_codex(self.agent.config, project):
            raise DevError('SESSION_POLICY_UNSUPPORTED', 'Stateful shells require unrestricted local shell policy', 403)
        binding = identity(project)
        async with self.open_lock:
            if identifier in self.handles:
                handle = self.handles[identifier]
                if handle.binding != binding:
                    raise DevError('SESSION_BINDING_CHANGED', 'A Session ID cannot be rebound', 409)
                return handle.status()
            if len(self.active()) >= settings['max_sessions']:
                raise DevError('SESSION_LIMIT', 'Close an existing Session before opening another', 409)
            tty = args['mode'] == 'pty'
            if tty and os.name not in {'posix', 'nt'}:
                raise DevError('PTY_PLATFORM_UNSUPPORTED', 'No terminal backend for this Runner platform', 409)
            remote = bool(project.get('_vps_context'))
            spec = (SSHBackend(self.agent) if remote else LocalBackend(self.agent)).plan(project, args, session=True, tty=tty)
            env = dict(os.environ) if spec.inherit_env else {'PATH': os.environ.get('PATH', os.defpath)}
            if os.name == 'nt' and not spec.inherit_env:
                env.update({key: os.environ[key] for key in ('SystemRoot', 'COMSPEC', 'TEMP', 'TMP', 'PATHEXT') if key in os.environ})
            env.update(spec.env)
            enforce_argv(self.agent.config, project, spec.argv, spec.cwd, env)
            master = slave = guard = process = None
            try:
                if tty and os.name == 'nt':
                    process = ConPTYProcess.start(spec.argv, spec.cwd, env, args['rows'], args['cols'])
                elif tty:
                    import fcntl
                    import termios
                    master, slave = os.openpty()
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', args['rows'], args['cols'], 0, 0))
                    os.set_blocking(master, False)
                    argv = [sys.executable, str(Path(__file__).with_name('pty_child.py')), *spec.argv]
                    process = await asyncio.create_subprocess_exec(*argv, cwd=spec.cwd, env=env,
                        stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                else:
                    process = await asyncio.create_subprocess_exec(*spec.argv, cwd=spec.cwd, env=env,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        **({'start_new_session': True} if os.name == 'posix' else {'creationflags': 0x00000200}))
                    if os.name == 'nt':
                        from agent.native_windows import ProcessJob
                        guard = ProcessJob(process.pid)
                handle = Handle(identifier, binding, args['mode'], process, remote,
                    target_fingerprint(self.agent.config, project) if remote else None, spec.shell, master=master, guard=guard, limits=dict(settings))
                self.handles[identifier] = handle
                handle.reader = asyncio.create_task(
                    self._windows_pty_output(handle) if isinstance(process, ConPTYProcess)
                    else self._pty_output(handle) if tty else self._stderr(handle))
                if not tty and spec.shell != 'powershell':
                    setup = ("umask 077\n__cp_dir=$(mktemp -d) || exit 70\n"
                             "trap 'rm -rf -- \"$__cp_dir\"' EXIT\n"
                             "printf 'CPREADY\\n'\n")
                    process.stdin.write(setup.encode())
                    await process.stdin.drain()
                if not tty:
                    if await asyncio.wait_for(process.stdout.readline(), 20) != b'CPREADY\n':
                        raise DevError('SESSION_PROTOCOL_FAILED', 'The saved target did not establish its framed shell protocol', 409)
                if self.reaper is None or self.reaper.done():
                    self.reaper = asyncio.create_task(self._reap())
                return handle.status()
            except BaseException:
                if identifier in self.handles:
                    await self._close(self.handles[identifier], 'lost')
                else:
                    if process is not None and process.returncode is None:
                        process.kill()
                        await process.wait()
                    if guard is not None:
                        guard.close()
                    if isinstance(process, ConPTYProcess):
                        process.release()
                    if master is not None:
                        os.close(master)
                raise
            finally:
                if slave is not None:
                    os.close(slave)

    def _require_input_policy(self, project):
        if not self.agent.config.get('sessions', {}).get('enabled'):
            raise DevError('SESSIONS_DISABLED', 'This Runner no longer allows Session input', 403)
        if agent_blocks_codex(self.agent.config, project):
            raise DevError('SESSION_POLICY_UNSUPPORTED', 'This Runner no longer permits unrestricted Session input', 403)

    async def _command(self, handle, identifier, args, project):
        async with handle.lock:
            # A command may have waited behind another command while local
            # policy changed. Admission alone does not authorize later input.
            self._require_input_policy(project)
            if handle.status()['state'] != 'active':
                raise DevError('SESSION_LOST', 'The original shell is no longer running', 409)
            if self.cancelled(identifier):
                return {**handle.status(),'command_id':identifier,'cancelled':True,'outcome_uncertain':False,'exit_code':None}
            handle.busy = True
            self.executing[identifier]=handle
            limit = min(args['timeout_seconds'], self.agent.config['ssh' if handle.remote else 'shell']['max_timeout_seconds'])
            # Per-command files are removed after the fixed-length payload is
            # transferred. No user stdout/ANSI text can masquerade as completion.
            script = (
                f"eval {shlex.quote(args['command'])} </dev/null >\"$__cp_dir/{identifier}.out\" 2>\"$__cp_dir/{identifier}.err\"\n"
                "__cp_status=$?\n"
                f'__cp_out=$(wc -c <"$__cp_dir/{identifier}.out") && __cp_out=$(printf "%d" "$__cp_out") || exit 70\n'
                f'__cp_err=$(wc -c <"$__cp_dir/{identifier}.err") && __cp_err=$(printf "%d" "$__cp_err") || exit 70\n'
                # wc on supported POSIX platforms may pad its numeric output.
                # Emit canonical decimal fields; never relax the receive parser
                # or reinterpret user stdout as a control message.
                f"printf 'CP1 {identifier} %d %d %d\\n' \"$__cp_status\" \"$__cp_out\" \"$__cp_err\"\n"
                f'__cp_n=$__cp_out; [ "$__cp_n" -le {OUTPUT_LIMIT} ] || __cp_n={OUTPUT_LIMIT}; head -c "$__cp_n" "$__cp_dir/{identifier}.out"\n'
                f'__cp_n=$__cp_err; [ "$__cp_n" -le {OUTPUT_LIMIT} ] || __cp_n={OUTPUT_LIMIT}; head -c "$__cp_n" "$__cp_dir/{identifier}.err"\n'
                f'rm -f -- "$__cp_dir/{identifier}.out" "$__cp_dir/{identifier}.err"\n')
            async def command():
                if handle.shell == 'powershell' and not handle.remote:
                    from agent.session_powershell import command as powershell_command
                    payload = powershell_command(identifier, args['command'])
                else:
                    payload = script.encode()
                handle.process.stdin.write(payload)
                await handle.process.stdin.drain()
                header = await handle.process.stdout.readline()
                pattern = rb'CP1 ' + identifier.encode() + rb' (-?[0-9]{1,10}) ([0-9]{1,20}) ([0-9]{1,20})\n'
                match = re.fullmatch(pattern, header)
                if match is None:
                    raise DevError('SESSION_PROTOCOL_LOST', 'The original shell ended or broke its control stream', 409)
                status, out_length, err_length = map(int, match.groups())
                stdout = await handle.process.stdout.readexactly(min(out_length, OUTPUT_LIMIT))
                stderr = await handle.process.stdout.readexactly(min(err_length, OUTPUT_LIMIT))
                handle.output.append(stdout + stderr)
                return {**handle.status(), 'command_id': identifier, 'exit_code': status,
                        'stdout': stdout.decode('utf-8', 'replace'), 'stderr': stderr.decode('utf-8', 'replace'),
                        'stdout_base64': base64.b64encode(stdout).decode(), 'stderr_base64': base64.b64encode(stderr).decode(),
                        'output': (stdout + stderr).decode('utf-8', 'replace'),
                        'output_truncated': max(out_length, err_length) > OUTPUT_LIMIT}
            try:
                return await asyncio.wait_for(command(), limit)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, BrokenPipeError, ConnectionError, DevError) as exc:
                await self._close(handle, 'lost')
                return {**handle.status(), 'command_id': identifier, 'exit_code': None,
                        'timed_out': isinstance(exc, asyncio.TimeoutError), 'cancelled': self.cancelled(identifier), 'outcome_uncertain': True,
                        'remote_execution_uncertain': handle.remote,
                        'error': 'The original command outcome is unconfirmed; do not replay it'}
            except asyncio.CancelledError:
                await self._close(handle, 'lost')
                raise
            finally:
                self.executing.pop(identifier,None)
                handle.busy = False
                handle.touched = time.monotonic()

    async def execute(self, identifier, project, args):
        if args['operation'] == 'open':
            return await self.open(identifier, project, args)
        handle = self.handles.get(args['session_id'])
        if handle is None or handle.binding != identity(project):
            raise DevError('SESSION_LOST', 'No live handle for this connection; a restart does not restore shell state', 409)
        operation = args['operation']
        # Existing handles do not retain a privilege snapshot. Recheck the
        # current Runner/Hub policy before sending any new executable input.
        # Read/status/close still operate on the original handle for cleanup.
        if operation in {'exec', 'write'}:
            self._require_input_policy(project)
        if operation not in {'read', 'status', 'close'}:
            if not self.agent.config.get('sessions', {}).get('enabled'):
                raise DevError('SESSIONS_DISABLED', 'This Runner no longer allows Session input', 403)
            if handle.status()['state'] != 'active':
                raise DevError('SESSION_LOST', 'The original shell is no longer active', 409)
            if handle.remote:
                if target_fingerprint(self.agent.config, project) != handle.config_hash:
                    raise DevError('SESSION_TARGET_CHANGED', 'SSH settings changed; the original Session cannot be retargeted', 409)
            else:
                LocalBackend(self.agent).plan(project, {'cwd': '.', 'env': {}, 'shell': handle.shell}, session=True, tty=handle.mode == 'pty')
        handle.touched = time.monotonic()
        if operation == 'status':
            return handle.status()
        if operation == 'read':
            return {**handle.status(), **handle.output.read(args['cursor'], args['limit'])}
        if operation == 'close':
            await self._close(handle)
            return handle.status()
        if operation == 'exec':
            if handle.mode != 'persistent_shell':
                raise DevError('SESSION_MODE_MISMATCH', 'PTYs use raw write/read, not per-command exec')
            return await self._command(handle, identifier, args, project)
        if operation == 'signal':
            if args['signal'] == 'interrupt':
                if isinstance(handle.process, ConPTYProcess):
                    count = await asyncio.wait_for(handle.process.write(b'\x03'), 10)
                    if count != 1:
                        return {**handle.status(), 'outcome_uncertain': True, 'error': 'The terminal did not confirm its interrupt input'}
                elif handle.master is not None:
                    os.write(handle.master, b'\x03')
                elif os.name == 'nt':
                    handle.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(handle.process.pid, signal.SIGINT)
            else:
                await self._close(handle, 'lost')
            return handle.status()
        terminal = isinstance(handle.process, ConPTYProcess)
        if handle.mode != 'pty' or handle.master is None and not terminal:
            raise DevError('SESSION_MODE_MISMATCH', 'Raw input and resize require an active PTY')
        if operation == 'resize':
            if terminal:
                handle.process.resize(args['rows'], args['cols'])
            else:
                import fcntl
                import termios
                fcntl.ioctl(handle.master, termios.TIOCSWINSZ, struct.pack('HHHH', args['rows'], args['cols'], 0, 0))
            return {**handle.status(), 'rows': args['rows'], 'cols': args['cols']}
        if operation == 'write':
            data = base64.b64decode(args['data_base64'], validate=True) if args.get('data_base64') else args['data'].encode()
            async with handle.lock:
                self._require_input_policy(project)
                sent = 0
                if self.cancelled(identifier):
                    return {**handle.status(), 'bytes_written': 0, 'cancelled': True, 'outcome_uncertain': False}
                self.executing[identifier] = handle
                try:
                    async with asyncio.timeout(10):
                        if terminal:
                            # ConPTY reports the original UTF-8 write length. A
                            # partial/ambiguous write is not automatically resent.
                            sent = await handle.process.write(data)
                            return {**handle.status(), 'bytes_written': sent, 'outcome_uncertain': sent != len(data)}
                        while sent < len(data):
                            if self.cancelled(identifier):
                                return {**handle.status(),'bytes_written':sent,'cancelled':True,'outcome_uncertain':bool(sent)}
                            try:
                                sent += os.write(handle.master, data[sent:])
                            except BlockingIOError:
                                await asyncio.sleep(0.01)
                except (TimeoutError, OSError):
                    if terminal:
                        await self._close(handle, 'lost')
                    return {**handle.status(), 'bytes_written': sent, 'outcome_uncertain': True,
                            'error': 'PTY input may be partial; inspect the original operation, never resend automatically'}
                finally:
                    self.executing.pop(identifier, None)
                return {**handle.status(), 'bytes_written': sent}
        raise DevError('UNKNOWN_SESSION_OPERATION', 'Unknown Session operation')

    async def close(self):
        if self.reaper:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        await asyncio.gather(*(self._close(h) for h in self.handles.values()))
