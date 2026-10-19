"""Framing and portable stdio workers, without a Hub or WebSocket listener."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading

import pytest

from shared.local_transport import MAX_FRAME_SIZE, PipeTransport, StdioTransport

REPO = Path(__file__).resolve().parents[1]


def frame(text):
    payload = text.encode("utf-8")
    return struct.pack("!I", len(payload)) + payload


class Writer:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.transport = self
        self.release = asyncio.Event()
        self.release.set()
        self.draining = asyncio.Event()

    def write(self, value):
        self.writes.append(bytes(value))

    async def drain(self):
        self.draining.set()
        await self.release.wait()

    def close(self):
        self.closed = True

    def abort(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_fragmented_unicode_frame_aliases_and_iterator():
    reader, writer = asyncio.StreamReader(), Writer()
    transport = PipeTransport(reader, writer)
    pending = asyncio.create_task(transport.receive_text())
    for byte in frame("hello 世界"):
        reader.feed_data(bytes([byte]))
        await asyncio.sleep(0)
    assert await pending == "hello 世界"
    reader.feed_data(frame("next"))
    assert await anext(transport) == "next"
    await transport.send_json({"hello": "世界"})
    assert b"".join(writer.writes) == frame('{"hello":"世界"}')
    await transport.send_text("last")
    reader.feed_eof()
    with pytest.raises(ConnectionError):
        await anext(transport)
    assert writer.closed
    with pytest.raises(ConnectionError):
        await transport.send("poisoned")


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [b"", b"\x00", struct.pack("!I", 5) + b"hi",
                                   struct.pack("!I", 1) + b"\xff"])
async def test_eof_truncation_and_bad_utf8_poison_channel(wire):
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(wire)
    reader.feed_eof()
    transport = PipeTransport(reader, writer)
    with pytest.raises(ConnectionError):
        await transport.recv()
    with pytest.raises(ConnectionError):
        await transport.recv()
    assert writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [0, MAX_FRAME_SIZE + 1, 0xffffffff])
async def test_invalid_length_rejected_before_body_read(length):
    class HeaderOnly:
        calls = []

        async def readexactly(self, n):
            self.calls.append(n)
            assert n == 4, "invalid size must never reach body allocation"
            return struct.pack("!I", length)

    reader, writer = HeaderOnly(), Writer()
    transport = PipeTransport(reader, writer)
    with pytest.raises(ConnectionError):
        await transport.recv()
    assert reader.calls == [4] and writer.closed


@pytest.mark.asyncio
async def test_size_limit_in_bytes_and_boundary_roundtrip():
    reader, writer = asyncio.StreamReader(), Writer()
    transport = PipeTransport(reader, writer)
    boundary = "a" * MAX_FRAME_SIZE
    await transport.send(boundary)
    reader.feed_data(b"".join(writer.writes))
    assert await transport.recv() == boundary
    with pytest.raises(ConnectionError):
        await transport.send("世" * (MAX_FRAME_SIZE // 3 + 1))
    assert writer.closed


@pytest.mark.asyncio
async def test_sends_serialized_and_backpressure_cancellation_is_terminal():
    writer = Writer()
    writer.release.clear()
    transport = PipeTransport(asyncio.StreamReader(), writer)
    first = asyncio.create_task(transport.send("a" * (1024 * 1024)))
    await writer.draining.wait()
    second = asyncio.create_task(transport.send("second"))
    await asyncio.sleep(.01)
    assert len(b"".join(writer.writes)) == 4 + 65536
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(ConnectionError):
        await second
    assert writer.closed and not transport._pending


@pytest.mark.asyncio
async def test_concurrent_sends_do_not_interleave():
    writer = Writer()
    transport = PipeTransport(asyncio.StreamReader(), writer)
    messages = [str(i) * 70000 for i in range(8)]
    await asyncio.gather(*(transport.send(text) for text in messages))
    assert b"".join(writer.writes) == b"".join(frame(text) for text in messages)
    await transport.close()


@pytest.mark.asyncio
async def test_cancelling_partial_receive_poisoned_and_close_wakes_reader():
    reader, writer = asyncio.StreamReader(), Writer()
    transport = PipeTransport(reader, writer)
    reader.feed_data(struct.pack("!I", 3) + b"a")
    pending = asyncio.create_task(transport.recv())
    await asyncio.sleep(.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with pytest.raises(ConnectionError):
        await transport.recv()

    transport = PipeTransport(asyncio.StreamReader(), Writer())
    pending = asyncio.create_task(transport.recv())
    await asyncio.sleep(.01)
    await transport.close(code=1008, reason="revoked")
    with pytest.raises(ConnectionError, match="revoked"):
        await pending
    assert not transport._pending


@pytest.mark.asyncio
@pytest.mark.parametrize('interruption', ['cancel', 'timeout'])
@pytest.mark.parametrize('failure', [False, True])
async def test_close_waiter_survives_interrupted_caller(interruption, failure):
    class DelayedWriter(Writer):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.finish = asyncio.Future()
            self.waits = 0
            self.aborts = 0

        def abort(self):
            self.aborts += 1
            super().abort()

        async def wait_closed(self):
            self.waits += 1
            self.started.set()
            await self.finish

    writer = DelayedWriter()
    transport = PipeTransport(asyncio.StreamReader(), writer)
    closing = asyncio.create_task(transport.close(reason='first reason'))
    await writer.started.wait()
    if interruption == 'cancel':
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        await asyncio.wait_for(closing, 2)
    assert not writer.finish.cancelled()
    assert not transport._close_waiter.done()
    if failure:
        writer.finish.set_exception(BrokenPipeError('peer closed'))
    else:
        writer.finish.set_result(None)
    await transport.close(reason='later reason')
    assert writer.waits == writer.aborts == 1
    assert transport._error == 'first reason'


@contextlib.asynccontextmanager
async def real_parent_pipe(*, reads=True):
    loop = asyncio.get_running_loop()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))
    code = 'import sys; sys.stdin.buffer.read()' if reads else 'import time; time.sleep(30)'
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    transport = PipeTransport(asyncio.StreamReader(), child.stdin)
    try:
        yield transport
    finally:
        try:
            await transport.close()
            if child.returncode is None:
                child.terminate()
            await asyncio.wait_for(child.communicate(), 5)
            # Observe callback failures after connection_lost and concurrent
            # cleanup callbacks, not just the result of the close coroutine.
            for _ in range(3):
                barrier = loop.create_future()
                loop.call_soon(barrier.set_result, None)
                await barrier
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()
            loop.set_exception_handler(previous_handler)
        assert not errors, [(item.get('message'), repr(item.get('exception'))) for item in errors]


@pytest.mark.asyncio
async def test_real_pipe_invalid_frame_does_not_double_finalize():
    async with real_parent_pipe() as transport:
        transport.reader.feed_data(struct.pack('!I', 0))
        with pytest.raises(ConnectionError):
            await transport.recv()
        await asyncio.gather(transport.close(), transport.close())


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_reader', [False, True])
async def test_real_pipe_pending_reader_and_repeated_close(cancel_reader):
    async with real_parent_pipe() as transport:
        started = asyncio.Event()
        original = transport.reader.readexactly

        async def read(n):
            started.set()
            return await original(n)

        transport.reader.readexactly = read
        reading = asyncio.create_task(transport.recv())
        await started.wait()
        if cancel_reader:
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
        else:
            await transport.close(reason='roots changed')
            with pytest.raises(ConnectionError, match='roots changed'):
                await reading
        await asyncio.gather(transport.close(), transport.close())
        assert not transport._pending


@pytest.mark.asyncio
@pytest.mark.parametrize('completed', [False, True])
async def test_real_pipe_already_closing_or_closed_is_not_aborted_again(completed):
    async with real_parent_pipe() as transport:
        transport.writer.close()
        if completed:
            await transport.writer.wait_closed()
        transport.reader.feed_eof()
        with pytest.raises(ConnectionError):
            await transport.recv()
        await transport.close()


@pytest.mark.asyncio
async def test_real_pipe_buffered_graceful_close_can_be_aborted_once():
    async with real_parent_pipe(reads=False) as transport:
        transport.writer.write(b'x' * (1024 * 1024))
        assert transport.writer.transport.get_write_buffer_size() > 0
        transport.writer.close()
        await asyncio.wait_for(asyncio.gather(transport.close(), transport.close()), 2)
        assert transport.writer.transport.get_write_buffer_size() == 0


@contextlib.contextmanager
def bridge(**kwargs):
    read_fd, parent_write = os.pipe()
    parent_read, write_fd = os.pipe()
    stopped, exited = threading.Event(), threading.Event()
    transport = StdioTransport(read_fd, write_fd, asyncio.get_running_loop(), stopped.set,
                               terminate=lambda code: exited.set(), **kwargs)
    try:
        yield transport, parent_write, parent_read, stopped, exited
    finally:
        transport.finish()
        os.close(parent_write)
        os.close(parent_read)
        transport._reader.join(1)
        transport._writer.join(1)
        assert not transport._reader.is_alive()
        assert not transport._writer.is_alive()


@pytest.mark.asyncio
async def test_child_worker_unicode_serialized_sends_and_single_reader():
    with bridge() as (transport, parent_write, parent_read, stopped, exited):
        wire = frame("one 世界")
        pending = asyncio.create_task(transport.recv())
        for byte in wire:
            os.write(parent_write, bytes([byte]))
            await asyncio.sleep(0)
        assert await asyncio.wait_for(pending, 1) == "one 世界"
        await asyncio.gather(transport.send("first"), transport.send("second"))
        assert os.read(parent_read, 1024) == frame("first") + frame("second")
        assert not stopped.is_set() and not exited.is_set()
        assert transport._reader.daemon and transport._writer.daemon


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [struct.pack("!I", 0), struct.pack("!I", MAX_FRAME_SIZE + 1),
                                   struct.pack("!I", 1) + b"\xff"])
async def test_child_bad_frames_stop_without_body_allocation(wire):
    with bridge(exit_timeout=.05) as (transport, parent_write, parent_read, stopped, exited):
        os.write(parent_write, wire)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(transport.recv(), 1)
        await asyncio.sleep(.01)
        assert stopped.is_set()
        with pytest.raises(ConnectionError):
            await transport.send("no recovery")


@pytest.mark.asyncio
async def test_receive_handoff_bounded_even_with_stalled_event_loop():
    with bridge(handoff_timeout=.1, exit_timeout=.1) as (transport, parent_write, _, stopped, exited):
        os.write(parent_write, frame("one") + frame("two") + frame("three"))
        # Deliberately starve loop callbacks; daemon guard remains independent.
        assert exited.wait(1)
        assert transport._incoming.qsize() == 1
        assert not stopped.is_set()
        await asyncio.sleep(.01)
        assert stopped.is_set()


@pytest.mark.asyncio
async def test_child_blocked_writer_cancelled_without_executor_threads():
    with bridge() as (transport, _, parent_read, stopped, exited):
        sending = asyncio.create_task(transport.send("x" * MAX_FRAME_SIZE))
        await asyncio.sleep(.03)
        assert not sending.done()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        with pytest.raises(ConnectionError):
            await transport.send("not retryable")
        assert transport._outgoing.qsize() <= 1


@pytest.mark.asyncio
async def test_close_wakes_parent_blocked_send_and_child_receive():
    writer = Writer()
    writer.release.clear()
    transport = PipeTransport(asyncio.StreamReader(), writer)
    sending = asyncio.create_task(transport.send("blocked"))
    await writer.draining.wait()
    await transport.close()
    with pytest.raises(ConnectionError):
        await sending
    assert not transport._pending
    with bridge() as (transport, _, __, stopped, exited):
        receiving = asyncio.create_task(transport.recv())
        await asyncio.sleep(.01)
        await transport.close()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(receiving, 1)


@pytest.mark.asyncio
async def test_child_cancel_receive_is_terminal_and_guard_disarmed_only_after_finish():
    with bridge(exit_timeout=.05) as (transport, _, __, stopped, exited):
        receiving = asyncio.create_task(transport.recv())
        await asyncio.sleep(.01)
        receiving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receiving
        with pytest.raises(ConnectionError):
            await transport.recv()
        transport.finish()
        await asyncio.sleep(.1)
        assert not exited.is_set()


def test_real_stdio_redirects_print_fd1_existing_logs_and_descendant_stdout():
    code = '''
import asyncio, faulthandler, logging, os, subprocess, sys
from shared.local_transport import StdioTransport
# A failed native shutdown must report which owned worker/CRT operation is
# blocked. This timer belongs only to the isolated test subprocess.
faulthandler.dump_traceback_later(5, file=sys.stderr)
logger = logging.getLogger("fixture")
logger.addHandler(logging.StreamHandler(sys.stdout))
async def run():
    with StdioTransport.from_stdio(asyncio.get_running_loop(), lambda: None) as transport:
        assert not os.get_inheritable(transport._writer._args[0])
        assert not os.get_inheritable(transport._reader._args[0])
        # The private framing reader owns the parent's protocol pipe. Neither
        # ordinary Python stdin nor a descendant may read or inherit it.
        assert os.read(0, 1) == b''
        print("python diagnostic", flush=True)
        os.write(1, b"fd1 diagnostic\\n")
        logger.warning("existing logger")
        subprocess.run([sys.executable, "-c",
            "import sys; assert sys.stdin.read() == ''; print('descendant diagnostic')"],
            check=True, timeout=5)
        assert await transport.recv() == 'private protocol request'
        await transport.send("only protocol 世界")
    if os.name=='nt':
        assert not transport._reader.is_alive() and not transport._writer.is_alive()
        assert not transport._windows_io.handles
asyncio.run(run())
faulthandler.cancel_dump_traceback_later()
'''
    # Keep the protocol input pipe open even after sending one frame. Ordinary
    # descendants must observe EOF on their own stdin without stealing a frame
    # or blocking Python startup on the original synchronous pipe handle.
    child = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        child.stdin.write(frame('private protocol request'))
        child.stdin.flush()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            _output, diagnostic=child.communicate(timeout=2)
            pytest.fail('Isolated stdio child failed to exit with stdin open:\n'+diagnostic.decode(errors='replace'))
        stdout, stderr = child.communicate(timeout=2)
        assert child.returncode == 0, stderr.decode()
        assert stdout == frame("only protocol 世界")
        for text in (b"python diagnostic", b"fd1 diagnostic", b"existing logger", b"descendant diagnostic"):
            assert text in stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=2)


@pytest.mark.parametrize("prefix", [b"", b"\x00", struct.pack("!I", 9) + b"short"])
def test_parent_eof_hard_exit_independent_of_startup_or_loop(prefix):
    code = '''
import asyncio, os, time
from shared.local_transport import StdioTransport
async def run():
    transport = StdioTransport.from_stdio(asyncio.get_running_loop(), lambda: None)
    transport._exit_timeout = .1
    os.write(2, b"guard-ready\\n")
    time.sleep(30)
asyncio.run(run())
'''
    child = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert child.stderr.readline() == b"guard-ready\n"
        child.stdin.write(prefix)
        child.stdin.close()
        child.wait(timeout=3)
        assert child.returncode == 0
        assert child.stdout.read() == b""
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        child.stdout.close()
        child.stderr.close()
