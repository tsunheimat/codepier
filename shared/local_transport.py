"""Private framed stdio transport; no listener and no alternate execution path.

Frames are a four-byte unsigned network-order length followed by UTF-8 text.
A failed/cancelled operation poisons the stream: a partially consumed frame or
SecureChannel sequence must never be retried on the same channel.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import struct
import sys
import threading
import time

MAX_FRAME_SIZE = 16 * 1024 * 1024
_IO_CHUNK = 64 * 1024


def _length(header):
    length = struct.unpack("!I", header)[0]
    if not 0 < length <= MAX_FRAME_SIZE:
        raise ConnectionError("Invalid local frame length")
    return length


def _encode(text):
    if not isinstance(text, str):
        raise ConnectionError("Local frames must be text")
    if not 0 < len(text) <= MAX_FRAME_SIZE:
        raise ConnectionError("Invalid local frame length")
    payload = text.encode("utf-8")
    if len(payload) > MAX_FRAME_SIZE:
        raise ConnectionError("Local frame too large")
    return payload


class _TextTransport:
    async def receive_text(self):
        return await self.recv()

    async def send_text(self, text):
        await self.send(text)

    async def send_json(self, data):
        await self.send(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False))

    def __aiter__(self):
        return self

    async def __anext__(self):
        # EOF deliberately raises ConnectionError, including in an iterator.
        return await self.recv()


class PipeTransport(_TextTransport):
    """Parent endpoint over asyncio subprocess stdout/stdin, respectively.

    Only one encoded send is retained; drain after each small write bounds the
    subprocess pipe buffer even when a peer stops reading. close wakes pending
    operations and never waits indefinitely for that peer to drain its input.
    """

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self._send_lock = asyncio.Lock()
        self._recv_lock = asyncio.Lock()
        self._error = None
        self._pending = set()
        self._close_waiter = None

    def _check(self):
        if self._error is not None:
            raise ConnectionError(self._error)

    def _poison(self, error):
        if self._error is not None:
            return
        self._error = str(error)
        for task in self._pending:
            task.cancel()
        transport = getattr(self.writer, "transport", None)
        if transport is None:
            self.writer.close()
            return
        # Unix write pipes can schedule connection_lost twice for close+abort.
        # A closing empty pipe has already scheduled/completed finalization; a
        # buffered graceful close still needs abort to release a stalled writer.
        closing = getattr(transport, "is_closing", lambda: False)()
        buffered = getattr(transport, "get_write_buffer_size", lambda: 0)()
        if not closing or buffered:
            transport.abort()

    async def _operate(self, operation):
        task = asyncio.create_task(operation)
        self._pending.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            closed = self._error is not None
            self._poison("Local operation cancelled")
            if closed and not asyncio.current_task().cancelling():
                raise ConnectionError(self._error) from None
            raise
        except Exception as exc:
            self._poison(exc)
            raise ConnectionError("Local pipe failed") from exc
        finally:
            self._pending.discard(task)

    async def _read_frame(self):
        length = _length(await self.reader.readexactly(4))
        # Validate the advertised size before asking the reader for its body.
        return (await self.reader.readexactly(length)).decode("utf-8")

    async def recv(self):
        async with self._recv_lock:
            self._check()
            return await self._operate(self._read_frame())

    async def _write_frame(self, text):
        payload = _encode(text)
        self.writer.write(struct.pack("!I", len(payload)))
        view = memoryview(payload)
        for offset in range(0, len(view), _IO_CHUNK):
            self.writer.write(view[offset:offset + _IO_CHUNK])
            await self.writer.drain()

    async def send(self, text):
        async with self._send_lock:
            self._check()
            await self._operate(self._write_frame(text))

    async def close(self, code=1000, reason=""):
        self._poison(reason or "Local pipe closed")
        if self._close_waiter is None:
            self._close_waiter = asyncio.create_task(self.writer.wait_closed())
            self._close_waiter.add_done_callback(self._observe_close)
        try:
            async with asyncio.timeout(1):
                await asyncio.shield(self._close_waiter)
        except Exception:
            # The stream is already terminal; timeout or a broken peer must not
            # schedule another abort or cancel asyncio's shared close waiter.
            pass

    @staticmethod
    def _observe_close(task):
        if not task.cancelled():
            task.exception()


def _read_exactly(fd, size):
    # Called only after a validated header (or with size=4 for that header).
    data = bytearray(size)
    offset = 0
    while offset < size:
        chunk = os.read(fd, min(size - offset, _IO_CHUNK))
        if not chunk:
            raise ConnectionError("Local pipe EOF" if offset == 0 else "Truncated local frame")
        data[offset:offset + len(chunk)] = chunk
        offset += len(chunk)
    return data


class _WindowsSynchronousIO:
    """Cancel only these dedicated I/O workers, using owned thread handles.

    CancelSynchronousIo marks the call, not the thread, for cancellation. A
    duplicated handle avoids the recycled native thread-ID race at shutdown.
    https://learn.microsoft.com/windows/win32/api/ioapiset/nf-ioapiset-cancelsynchronousio
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes,self.handle_type=ctypes,wintypes.HANDLE
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        declarations={
            'GetCurrentProcess':([],wintypes.HANDLE),
            'GetCurrentThread':([],wintypes.HANDLE),
            'DuplicateHandle':([wintypes.HANDLE,wintypes.HANDLE,wintypes.HANDLE,
                ctypes.POINTER(wintypes.HANDLE),wintypes.DWORD,wintypes.BOOL,wintypes.DWORD],wintypes.BOOL),
            'CancelSynchronousIo':([wintypes.HANDLE],wintypes.BOOL),
            'CloseHandle':([wintypes.HANDLE],wintypes.BOOL),
        }
        for name,(arguments,result) in declarations.items():
            function=getattr(self.kernel,name);function.argtypes=arguments;function.restype=result
        self.handles=set();self.lock=threading.Lock()

    def register(self):
        handle=self.handle_type();process=self.kernel.GetCurrentProcess()
        if not self.kernel.DuplicateHandle(process,self.kernel.GetCurrentThread(),process,
                self.ctypes.byref(handle),0x0001,False,0):  # THREAD_TERMINATE, required by cancellation
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        with self.lock:self.handles.add(handle.value)
        return handle.value

    def release(self,handle):
        if handle is None:return
        with self.lock:
            self.handles.remove(handle)
            self.kernel.CloseHandle(handle)

    def cancel(self):
        # No thread pool and no TerminateThread. Cancel may race completed I/O;
        # retrying while these same dedicated workers drain covers that window.
        with self.lock:
            for handle in self.handles:
                self.kernel.CancelSynchronousIo(handle)


class StdioTransport(_TextTransport):
    """One child session, with bounded daemon I/O workers on POSIX and Windows.

    Workers, not the event loop/executor, own blocking reads and writes. Each
    direction hands off at most one frame. EOF/protocol errors request stop and
    arm an independent hard exit, even during imports, initialization or a stuck
    event loop. A full receive handoff also fails after a bounded stall: otherwise
    backpressure could hide parent EOF forever behind already-buffered frames.

    Use as a synchronous context around *all* Agent startup and cleanup. Only
    leaving that context disarms the hard-exit guard, not session close(). This
    process-lifetime endpoint is not reusable: a blocked OS worker is a daemon
    and releases its owned fd on pipe closure or process exit, never an executor
    thread that asyncio.run would wait for during shutdown.
    """

    def __init__(self, read_fd, write_fd, loop, stop, *, exit_timeout=8,
                 handoff_timeout=8, terminate=os._exit):
        self._loop, self._stop = loop, stop
        self._exit_timeout, self._handoff_timeout = exit_timeout, handoff_timeout
        self._terminate = terminate
        self._incoming = queue.Queue(maxsize=1)
        self._outgoing = queue.Queue(maxsize=1)
        self._read_ready = asyncio.Event()
        self._write_ready = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._recv_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._error = None
        self._windows_io=_WindowsSynchronousIO() if os.name=='nt' else None
        self._reader = threading.Thread(target=self._read_worker, args=(read_fd,),
                                        name="agent-stdio-reader", daemon=True)
        self._writer = threading.Thread(target=self._write_worker, args=(write_fd,),
                                        name="agent-stdio-writer", daemon=True)
        self._reader.start()
        self._writer.start()

    @classmethod
    def from_stdio(cls, loop, stop):
        # Reserve the protocol fd BEFORE redirecting fd 1. No Python stream or
        # descendant may inherit it, even when the descendant inherits stdout.
        write_fd = os.dup(1)
        read_fd = None
        try:
            os.set_inheritable(write_fd, False)
            read_fd = os.dup(0)
            os.set_inheritable(read_fd, False)
            if os.name == "nt":
                import msvcrt
                msvcrt.setmode(read_fd, os.O_BINARY)
                msvcrt.setmode(write_fd, os.O_BINARY)
            os.dup2(2, 1)
            if os.name == "nt":
                # CRT fd redirection alone does not reliably replace the Win32
                # standard handle used by native code / inherited child stdout.
                import ctypes
                from ctypes import wintypes
                set_handle = ctypes.WinDLL("kernel32", use_last_error=True).SetStdHandle
                set_handle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
                set_handle.restype = wintypes.BOOL
                if not set_handle(wintypes.DWORD(-11), msvcrt.get_osfhandle(1)):
                    raise ctypes.WinError(ctypes.get_last_error())
            previous = sys.stdout
            sys.stdout = sys.stderr
            # Buffered pre-initialization diagnostics now flush to stderr too.
            previous.flush()
            return cls(read_fd, write_fd, loop, stop)
        except BaseException:
            os.close(write_fd)
            if read_fd is not None:
                os.close(read_fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.finish()

    def _notify(self, callback):
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(callback)

    def _fail(self, error):
        with self._state_lock:
            if self._closed.is_set():
                return
            self._error = str(error)
            self._closed.set()

        def wake():
            self._read_ready.set()
            self._write_ready.set()
            if not self._finished.is_set():
                self._stop()

        self._notify(wake)
        # Do not wait on loop progress, logging locks, or a blocked writer.
        if not self._finished.is_set():
            def hard_exit():
                if not self._finished.wait(self._exit_timeout):
                    self._terminate(0)
            threading.Thread(target=hard_exit, name="agent-stdio-exit", daemon=True).start()
        with contextlib.suppress(queue.Full):
            self._outgoing.put_nowait(None)

    def _check(self):
        if self._closed.is_set():
            raise ConnectionError(self._error)

    def _read_worker(self, fd):
        handle=None
        try:
            if self._windows_io:handle=self._windows_io.register()
            while not self._closed.is_set():
                length = _length(_read_exactly(fd, 4))
                text = _read_exactly(fd, length).decode("utf-8")
                deadline = time.monotonic() + self._handoff_timeout
                while not self._closed.is_set():
                    try:
                        self._incoming.put(text, timeout=.1)
                        self._notify(self._read_ready.set)
                        break
                    except queue.Full:
                        if time.monotonic() >= deadline:
                            raise ConnectionError("Local receive handoff stalled")
        except Exception as exc:
            self._fail(exc)
        finally:
            os.close(fd)
            if self._windows_io:self._windows_io.release(handle)

    def _write_worker(self, fd):
        handle=None
        try:
            if self._windows_io:handle=self._windows_io.register()
            while not self._closed.is_set():
                payload = self._outgoing.get()
                if payload is None or self._closed.is_set():
                    break
                for part in (struct.pack("!I", len(payload)), payload):
                    view = memoryview(part)
                    while view:
                        written = os.write(fd, view[:_IO_CHUNK])
                        if written <= 0:
                            raise ConnectionError("Local pipe write failed")
                        view = view[written:]
                self._notify(self._write_ready.set)
        except Exception as exc:
            self._fail(exc)
        finally:
            os.close(fd)
            if self._windows_io:self._windows_io.release(handle)

    async def recv(self):
        async with self._recv_lock:
            try:
                while True:
                    self._check()
                    self._read_ready.clear()
                    try:
                        return self._incoming.get_nowait()
                    except queue.Empty:
                        await self._read_ready.wait()
            except asyncio.CancelledError:
                self._fail("Local receive cancelled")
                raise

    async def send(self, text):
        async with self._send_lock:
            self._check()
            try:
                self._write_ready.clear()
                self._outgoing.put_nowait(_encode(text))
                await self._write_ready.wait()
                self._check()
            except asyncio.CancelledError:
                self._fail("Local send cancelled")
                raise
            except Exception as exc:
                self._fail(exc)
                raise ConnectionError("Local pipe failed") from exc

    async def close(self, code=1000, reason=""):
        self._fail(reason or "Local pipe closed")

    def finish(self):
        if self._windows_io:
            # CRT finalization can wait on a descriptor locked by a blocked
            # _read even though Python's I/O worker is a daemon. Poison first,
            # cancel pending synchronous I/O, and let each owner close its fd.
            # The independent exit guard remains armed if cancellation fails.
            self._fail('Local stdio context finished')
            workers=(self._reader,self._writer)
            deadline=time.monotonic()+.5
            while any(worker.is_alive() for worker in workers) and time.monotonic()<deadline:
                self._windows_io.cancel()
                for worker in workers:worker.join(.005)
            if any(worker.is_alive() for worker in workers):
                raise ConnectionError('Windows local I/O workers did not finish after cancellation')
            self._finished.set()
        else:
            self._finished.set()
            self._fail('Local stdio context finished')
