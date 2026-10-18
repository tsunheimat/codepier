"""Read-only entrypoints retain runtime authorization and never launch work."""
import asyncio
import time

import pytest
from pydantic import ValidationError

from hub.core_tools import result as core_result
from hub.runtime import Runtime, Principal
from hub.store import Store
from shared.contracts import TOOLS, tool_definitions
from shared.crypto import token
from shared.query_contracts import QUERY_TOOLS
from shared.util import DevError
from tests.current_model import CurrentModel


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    model=CurrentModel(store);model.user('owner',username='admin')
    model.workspace('query-space',owner='owner');model.device('dev',owner='owner',name='home')
    model.bind_device('query-space','dev')
    source=tmp_path/'source';source.mkdir()
    folder=model.folder('query-folder',space='query-space',device='dev',path=source)
    for identifier,alias in [('visible','Visible'),('hidden','Hidden')]:
        model.project(identifier,space='query-space',folder=folder,owner='owner',alias=alias,subpath=identifier)
    profile=model.profile(space='query-space',creator='owner',policy={
        'project_rules':[{'projects':['visible'],'actions':['read']}]})
    _,principal=model.connection(user='owner',space='query-space',profile=profile)
    instance = Runtime(store)
    instance.wait_seconds = 0
    yield instance, principal
    store.close()


@pytest.mark.parametrize('profile', ['core', 'full', 'coding'])
def test_entrypoint_metadata_and_conservative_facades(profile):
    definitions = {item['name']: item for item in tool_definitions(profile)}
    assert QUERY_TOOLS <= definitions.keys()
    for name in QUERY_TOOLS:
        item = definitions[name]
        assert item['annotations']['readOnlyHint'] is True
        assert item['annotations']['destructiveHint'] is False
        assert item['annotations']['openWorldHint'] is False
    assert 'workbench' not in definitions
    for name in ('workspace', 'process', 'browser', 'computer'):
        assert definitions[name]['annotations']['readOnlyHint'] is False
    for name, definition in definitions.items():
        assert 'resourceUri' not in definition['_meta'].get('ui', {})
        assert 'openai/outputTemplate' not in definition['_meta']


@pytest.mark.parametrize('name,args', [
    ('project_query', {'operation': 'workflow_create'}),
    ('project_query', {'operation': 'project_create'}),
    ('project_query', {'operation': 'open', 'project': 'Visible', 'capture_baseline': True}),
    ('project_query', {'operation': 'open', 'project': 'Visible', 'options': {'capture_baseline': True}}),
    ('task_query', {'operation': 'cancel', 'operation_ids': ['original']}),
    ('task_query', {'operation': 'search_start', 'options': {'query': 'x'}}),
    ('task_query', {'operation': 'validate', 'options': {'command': 'true'}}),
])
def test_query_allowlists_reject_mutations(name, args):
    with pytest.raises(ValidationError):
        TOOLS[name].model.model_validate(args)


@pytest.mark.asyncio
async def test_project_query_filters_projects_and_revalidates_revoked_grants(runtime):
    instance, principal = runtime
    initial = await instance.invoke('project_query', {}, principal)
    assert [p['alias'] for p in initial['projects']] == ['Visible']
    assert not instance.store.all('SELECT id FROM operations')
    queried = await instance.invoke('project_query', {}, principal)
    assert queried == initial
    with pytest.raises(DevError):
        await instance.invoke('project_query', {'operation': 'open', 'project': 'Hidden'}, principal)
    assert not instance.store.all('SELECT id FROM operations')
    instance.store.execute('UPDATE grants SET revoked=1 WHERE id=?',(principal.grant_id,))
    with pytest.raises(DevError):
        await instance.invoke('project_query', {}, principal)


@pytest.mark.asyncio
async def test_query_keeps_batch_wait_parallel_and_existing_receipts(runtime, monkeypatch):
    instance, principal = runtime
    original = instance.invoke
    started = []
    both = asyncio.Event()
    async def invoke(name, args, user):
        if name == 'operations_wait':
            started.append(args['operation_id'])
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            return {'operation_id': args['operation_id'], 'state': 'running', 'pending': True}
        return await original(name, args, user)
    monkeypatch.setattr(instance, 'invoke', invoke)
    args = {'operation': 'wait', 'operation_ids': ['one', 'two']}
    value = await instance.invoke('task_query', args, principal)
    assert started == ['one', 'two']
    rendered = core_result('task_query', args, value)
    assert len(rendered['structuredContent']['operations']) == 2
    assert not instance.store.all('SELECT id FROM operations')
