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
import asyncio, logging, os, subprocess, sys
from shared.local_transport import StdioTransport
logger = logging.getLogger("fixture")
logger.addHandler(logging.StreamHandler(sys.stdout))
async def run():
    with StdioTransport.from_stdio(asyncio.get_running_loop(), lambda: None) as transport:
        assert not os.get_inheritable(transport._writer._args[0])
        print("python diagnostic", flush=True)
        os.write(1, b"fd1 diagnostic\\n")
        logger.warning("existing logger")
        subprocess.run([sys.executable, "-c", "print('descendant diagnostic')"], check=True)
        await transport.send("only protocol 世界")
asyncio.run(run())
'''
    # Keep parent stdin open while child produces its frame and exits normally.
    child = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        child.wait(timeout=10)
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
