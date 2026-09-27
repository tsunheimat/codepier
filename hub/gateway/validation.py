"""Bounded process isolation for backend-controlled regex/ref/combinator schemas."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from hub.gateway.catalog import encoded
from shared.util import DevError


class Validator:
    def __init__(self, limit=4, timeout=8):
        self.active = 0
        self.limit, self.timeout = limit, timeout

    async def validate(self, schema, value, code='GATEWAY_ARGUMENTS_INVALID'):
        if self.active >= self.limit:
            raise DevError('GATEWAY_BUSY', 'schema 验证容量已满；未发送新操作', 429)
        self.active += 1
        process = communication = spawning = None
        try:
            # Retain ownership even when cancellation races with process creation.
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, '-I', str(Path(__file__).with_name('validate_worker.py')),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL))
            process = await asyncio.shield(spawning)
            communication = asyncio.create_task(process.communicate(encoded({'schema': schema, 'value': value}).encode()))
            await asyncio.wait_for(asyncio.shield(communication), self.timeout)
            if process.returncode != 0:
                raise DevError(code, '数据不符合已发布 schema 或超出验证预算')
        except TimeoutError as exc:
            raise DevError(code, 'schema 验证超过时间预算') from exc
        except OSError as exc:
            raise DevError(code, '无法启动受限 schema 验证；未放宽验证') from exc
        finally:
            async def reap():
                child = process
                if child is None and spawning is not None:
                    try:
                        child = await spawning
                    except Exception:
                        return  # The original spawning error is propagated.
                if child is None:
                    return
                if child.returncode is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                if communication is not None:
                    await communication
                else:
                    await child.wait()
            cleanup = asyncio.create_task(reap())
            cancelled = False
            try:
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
            finally:
                self.active -= 1
            if cancelled:
                raise asyncio.CancelledError
