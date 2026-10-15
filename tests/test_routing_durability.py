"""Crash boundaries: persist source identity before dispatch, retain uncertain RCH jobs."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import routed_execution
from hub.store import Store
from shared.routing_contracts import RoutedExecution
from shared.util import DevError
from tests.routing_support import routing_stack


def test_route_identity_is_present_in_the_original_operation_insert(routing_stack):
    r = routing_stack
    store = Store(r.s.hubdir)
    try:
        # A subsequent UPDATE cannot satisfy this BEFORE INSERT invariant.
        store.execute("""CREATE TRIGGER fixture_route_is_atomic BEFORE INSERT ON operations
            WHEN NEW.tool='routed_exec' AND NEW.route_context IS NULL BEGIN
            SELECT RAISE(ABORT, 'routed call missing atomic source identity'); END""")
        result = r.exec([sys.executable, '-c', 'print("atomic routing")'])
        assert result['state'] == 'succeeded', result
        context = json.loads(result['route_context'])
        assert context['route_id'] == r.routes['test']['id']
        assert context['source_root_id'] == r.source['id']
        assert context['device_id'] == r.s.device
    finally:
        store.close()


@pytest.mark.asyncio
async def test_local_output_error_after_rch_launch_is_not_a_safe_retry(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    project = {'id': 'p', 'alias': 'P', 'root': str(root), 'mode': 'write', 'allow_tasks': True,
        '_integration_owner': 'grant:test', '_route_context': {'route_id': 'original-route'},
        '_execution_plan': {'hook_protocol': 1, 'provider': 'rch', 'rch': {}}}
    agent = SimpleNamespace(
        state_dir=tmp_path, cancelled=set(), stop_event=asyncio.Event(),
        config={'shell': {'enabled': True, 'projects': ['p'], 'env': {},
                         'inherit_env': False, 'max_timeout_seconds': 60}},
        engine=SimpleNamespace(root=lambda *a: (root, {'writable': True, 'allow_tasks': True}),
                               path=lambda *a: root),
        journal=SimpleNamespace(is_cancelled=lambda _: False),
        phase=lambda *a, **k: None,
        run_process=AsyncMock(side_effect=OSError('fixture output journal failure')))
    monkeypatch.setattr(routed_execution, 'local_rch', lambda _: '/deployment/rch')
    monkeypatch.setattr(routed_execution, 'probe_rch', AsyncMock())
    monkeypatch.setattr('shared.execution_policy.enforce_argv', lambda *a: None)
    args = RoutedExecution(project='p', action='test', argv=['python3', '-m', 'pytest'],
        timeout_seconds=30, idempotency_key='original-launch-id').model_dump()
    identifier = 'a' * 32
    with pytest.raises(DevError) as failure:
        await routed_execution.execute(agent, identifier, project, args)
    assert failure.value.code == 'RCH_EXECUTION_UNCERTAIN'
    assert agent.run_process.await_count == 1
    retained = json.loads((tmp_path / 'rch-operations' / identifier / 'codepier.json').read_text())
    assert retained['owner'] == 'grant:test'
    assert retained['route'] == {'route_id': 'original-route'}
    # Re-entering the same launch is refused even without a wrapper result.
    with pytest.raises(DevError) as repeated:
        await routed_execution.execute(agent, identifier, project, args)
    assert repeated.value.code == 'RCH_ALREADY_SUBMITTED'
    assert agent.run_process.await_count == 1


def test_routed_commands_preserve_source_and_declared_service_claims(tmp_path):
    from agent.resource_queue import Claim, claims_for, canonical_path
    engine = SimpleNamespace(config={})
    claims = claims_for(engine, 'routed_exec', {}, {'cwd': '.', 'resources': [
        {'kind': 'service', 'name': 'shared-test-database', 'mode': 'write'},
        {'kind': 'path', 'name': 'fixtures', 'mode': 'read'}]}, tmp_path)
    assert claims == [Claim('agent', 'path', canonical_path(tmp_path), True),
        Claim('agent', 'service', 'shared-test-database', True),
        Claim('agent', 'path', canonical_path(tmp_path / 'fixtures'), False)]


@pytest.mark.asyncio
async def test_routed_execution_waits_for_another_projects_service(tmp_path):
    from agent.resource_queue import ResourceQueue, Claim
    from agent.runner import Agent
    blocked, entered = asyncio.Event(), asyncio.Event()
    def phase(identifier, stage, **detail):
        if stage == 'waiting_resource' and detail.get('blocked_by'):
            blocked.set()
    instance = SimpleNamespace(engine=SimpleNamespace(config={}), resources=ResourceQueue(),
        semaphore=asyncio.Semaphore(1), read_semaphore=asyncio.Semaphore(1), phase=phase)
    args = {'cwd': '.', 'resources': [{'kind': 'service', 'name': 'shared-db', 'mode': 'write'}]}
    async def invoke():
        async with Agent.execution_slot(instance, 'routed', 'routed_exec', {}, args, tmp_path, True, None):
            entered.set()
    async with instance.resources.slot('another-project', [Claim('agent', 'service', 'shared-db', True)], lambda blockers: None):
        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            assert not entered.is_set()
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    await asyncio.wait_for(task, 2)
    assert entered.is_set()
