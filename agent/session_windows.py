"""Windows Session transport backed by ConPTY and retained process handles.

Reuse the native CLI's retained Job primitive, not its private sessions or
credentials. Session ConPTY owns its synchronous channels so every write has a
real byte-count acknowledgement. No plain-pipe fallback masquerades as a PTY.
"""
from __future__ import annotations

import asyncio
import threading

from agent.native_windows import ProcessJob
from agent.session_conpty import capabilities, create_pty
from shared.util import DevError


async def native_call(function, *args):
    """An input pipe may block; do not block the loop or its shutdown executor.

The process Job owns cancellation. A daemon worker retains the exact native
object until the call ends; cancellation never causes an automatic second write.
"""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def finish(value=None, error=None):
        if not result.done():
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)

    def run():
        try:
            value = function(*args)
        except Exception as exc:
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish, None, exc)
        else:
            if not loop.is_closed():
                loop.call_soon_threadsafe(finish, value)

    threading.Thread(target=run, name='codepier-session-io', daemon=True).start()
    return await result


class ConPTYProcess:
    stdin = None
    stdout = None
    stderr = None

    def __init__(self, native, guard):
        self.native, self.guard = native, guard
        self.pid = int(native.pid)
        self._exit_code = None

    @classmethod
    def start(cls, argv, cwd, env, rows, cols):
        availability = capabilities()
        if not availability['available']:
            raise DevError('PTY_PLATFORM_UNSUPPORTED', availability['reason'], 409)
        native = create_pty(argv, str(cwd), env, rows, cols)
        guard = None
        try:
            guard = ProcessJob(int(native.pid))
            native.resume()  # The child cannot execute before Job ownership exists.
        except BaseException:
            if guard is not None:
                guard.terminate()
                guard.close()
            native.close()
            raise
        return cls(native, guard)

    @property
    def returncode(self):
        if self.guard is not None:
            code = self.guard.exit_code()
            if code is not None:
                self._exit_code = code
        return self._exit_code

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.02)
        return self.returncode

    def terminate(self):
        if self.guard is not None:
            self.guard.terminate()

    kill = terminate

    def read(self):
        if self.native is None:
            return b''
        data = self.native.read(blocking=False)
        return data.encode('utf-8') if isinstance(data, str) else data

    async def write(self, data: bytes) -> int:
        try:
            text = data.decode('utf-8', errors='strict')
        except UnicodeDecodeError as exc:
            raise DevError('PTY_ENCODING_UNSUPPORTED', 'ConPTY input must be a complete UTF-8 sequence; no bytes were sent') from exc
        count = await native_call(self.native.write, text)
        if type(count) is not int or not 0 <= count <= len(data):
            raise OSError('ConPTY did not confirm its original write length')
        return count

    def resize(self, rows, cols):
        self.native.set_size(cols, rows)

    def release(self):
        if self.guard is not None:
            self._exit_code = self.guard.exit_code()
            self.guard.close()
            self.guard = None
        if self.native is not None:
            self.native.close()
            self.native = None
