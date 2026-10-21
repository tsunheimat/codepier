"""Primitive semantics and scheduler regressions in disposable projects only."""
import asyncio
import json
import time
import uuid
from dataclasses import replace

import pytest
from tests.vps_fixtures import observed_runner
from agent.runner import Agent
from agent.core_files import CoreFiles
from agent.resource_queue import Claim, ResourceQueue
from hub.runtime import Principal, Runtime
from hub.store import Store
from hub.vps import VPSInput, VPSBindingSave
from hub.core_tools import result as core_result
from scripts.mcp_stdio_bridge import prepare_request
from shared.contracts import TOOLS, tool_definitions
from shared.crypto import digest, token, password_hash
from shared.tool_protocol import wire_version, negotiate, require_compatible, advertisement
from shared.core_contracts import CORE_ACTIONS, REPLACED_MCP_TOOLS, CORE_TOOLS
from shared.query_contracts import QUERY_TOOLS
from shared.util import DevError, atomic_json
from tests.fake_computer_provider import png, frame
from shared.computer_media import normalize_content


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config = tmp_path / 'config.json'
    atomic_json(config, {'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture', 'secret': token(),
        'state_dir': str(tmp_path / 'state'),
        'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}], 'tasks': {}})
    instance = Agent(config)
    yield instance, {'root': str(root), 'alias': 'fixture', 'mode': 'write', 'allow_tasks': True}, root
    instance.journal.db.close()
    instance.instance_lock.close()


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    from hub.permissions import owner_policy
    now = time.time()
    root = tmp_path / 'fixture'
    root.mkdir()
    with store.transaction():
        store.db.execute('INSERT INTO users VALUES(?,?,?,?)', ('owner', 'admin', password_hash('test-workspace-password'), now))
        store.db.execute('INSERT INTO spaces(id,label,owner_user_id,created) VALUES(?,?,?,?)', ('fixture', 'Fixture workspace', 'owner', now))
        store.db.execute('INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)', ('fixture', 'owner', owner_policy().model_dump_json()))
        store.db.execute('INSERT INTO devices(id,name,secret,created,owner_user_id,info) VALUES(?,?,?,?,?,?)',
                         ('dev', 'home', store.encrypt(token()), now, 'owner', json.dumps({'shell_enabled': True})))
        store.db.execute('INSERT INTO space_devices(space_id,device_id) VALUES(?,?)', ('fixture', 'dev'))
        from tests.current_model import CurrentModel
        CurrentModel(store).folder('folder',space='fixture',device='dev',path=root,mode='write',allow_tasks=True)
        store.db.execute('INSERT INTO projects(id,space_id,alias,alias_key,source_id,owner_user_id,created) VALUES(?,?,?,?,?,?,?)',
                         ('proj', 'fixture', 'Fixture', 'fixture', 'folder', 'owner', now))
    instance = Runtime(store)
    instance.wait_seconds = 0
    principal = Principal('panel:admin', 'owner', {'read'}, [], space_id='fixture', user_epoch=1)
    yield instance, principal
    store.close()


def reader_principal(instance):
    # Authenticate a real immutable Connection -> Space + Profile binding.
    # No synthetic in-memory scope or obsolete grant can bypass live policy.
    from hub.auth import Auth
    from starlette.requests import Request
    now, profile_id = time.time(), 'prf_' + uuid.uuid4().hex
    policy = {'project_rules': [{'projects': ['proj'], 'actions': ['read']}]}
    instance.store.execute('INSERT INTO access_profiles(id,space_id,label,label_key,policy,created_by,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                           (profile_id, 'fixture', 'Reader', profile_id, json.dumps(policy), 'owner', now, now))
    auth = Auth(instance.store)
    owner = Principal('panel:admin', 'owner', {'read'}, [], space_id='fixture', user_epoch=1)
    connection = auth.issue_connection(owner, 'Core fixture reader', profile_id, 1)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/mcp',
                       'headers': [(b'authorization', ('Bearer ' + connection['token']).encode())]})
    return auth.bearer(request)


def request(project, name, **args):
    identifier = uuid.uuid4().hex
    model = TOOLS[name].model.model_validate({'project': project['alias'], **args})
    return {'id': identifier, 'tool': name, 'tool_contract_version': wire_version(name),
            'project': project, 'args': model.model_dump()}


def file_call(agent, name, **args):
    instance, project, _ = agent
    call = request(project, name, **args)
    return CoreFiles(instance.engine).call(name, project, call['args'])


def test_catalog_compact_and_protocol_boundaries():
    core = tool_definitions('core')
    assert {t['name'] for t in core} == CORE_TOOLS | QUERY_TOOLS | {'get_profile', 'get_access_context',
        'session', 'get_execution_options', 'sources_list', 'projects_create'}
    # The new Session/options/folder tools add schemas, not untyped catch-alls.
    assert len(json.dumps(core, ensure_ascii=False).encode()) < 110000
    assert {'browser', 'computer', 'exec', 'edit'} <= {t['name'] for t in tool_definitions('full')}
    assert not {'browser_open', 'computer_action', 'vps_exec', 'fs_read', 'shell_exec'} & {t['name'] for t in tool_definitions('full')}
    with pytest.raises(ValueError):
        negotiate({'version':'1.13.0'})
    frame=advertisement('a'*64);frame['tool_contracts']['exec']=1
    with pytest.raises(DevError,match='不兼容'):
        require_compatible(negotiate(frame),'exec')


def test_original_snapshot_edits_preserve_crlf_bom_and_backup(agent):
    _, _, root = agent
    before = '\ufeffalpha\r\nbeta\r\nunchanged\n'.encode()
    (root / 'file.txt').write_bytes(before)
    outcome = file_call(agent, 'edit', path='file.txt', expected_sha256=digest(before),
        edits=[{'old_text': 'alpha\nbeta', 'new_text': 'beta\nalpha'}], idempotency_key='edit-original')
    assert outcome['backup_id']
    assert (root / 'file.txt').read_bytes() == '\ufeffbeta\r\nalpha\r\nunchanged\n'.encode()
    with pytest.raises(DevError) as conflict:
        file_call(agent, 'write', path='file.txt', expected_sha256=digest(before), content='overwrite', idempotency_key='stale-update')
    assert conflict.value.code == 'SHA_CONFLICT'


@pytest.mark.parametrize('text,edits,code', [
    ('abc', [('a', 'x'), ('ab', 'y')], 'EDIT_OVERLAP'),
    ('aaa', [('aa', 'x')], 'EDIT_AMBIGUOUS'),
    ('a b', [('a', 'x'), ('x', 'y')], 'EDIT_NOT_FOUND'),
])
def test_failed_edit_is_atomic_and_matches_original(agent, text, edits, code):
    _, _, root = agent
    (root / 'file').write_text(text)
    with pytest.raises(DevError) as failure:
        file_call(agent, 'edit', path='file', expected_sha256=digest(text.encode()),
            edits=[{'old_text': old, 'new_text': new} for old, new in edits], idempotency_key='atomic-edit')
    assert failure.value.code == code and (root / 'file').read_text() == text


def test_write_parents_read_pages_and_images(agent):
    _, _, root = agent
    file_call(agent, 'write', path='nested/new.txt', expected_sha256='new', content='hello', idempotency_key='new-file-key')
    assert (root / 'nested/new.txt').read_text() == 'hello'
    (root / 'large.txt').write_text(('界' * 100 + '\n') * 7000)
    page = file_call(agent, 'read', path='large.txt')
    assert len(page['content'].encode()) <= 51200 and page['next_offset'] > 1
    next_page = file_call(agent, 'read', path='large.txt', offset=page['next_offset'], expected_sha256=page['sha256'])
    assert next_page['offset'] == page['end_line'] + 1
    (root / 'image.png').write_bytes(png())
    image = file_call(agent, 'read', path='image.png')
    rendered = core_result('read', {}, image)
    assert rendered['content'][1]['type'] == 'image'
    assert '_computer_content' not in rendered['structuredContent']
    for forbidden in ['../outside', '.env']:
        with pytest.raises(DevError):
            file_call(agent, 'read', path=forbidden)


@pytest.mark.asyncio
async def test_two_commands_start_together_and_reads_bypass_busy_workers(agent, monkeypatch):
    instance, project, root = agent
    release = asyncio.Event()
    both = asyncio.Event()
    started = []
    real_execute = instance.execute
    async def execution(identifier, tool, current, args):
        if tool == 'exec':
            started.append(identifier)
            if len(started) == 2:
                both.set()
            await release.wait()
            return {'exit_code': 0, 'output': 'done'}
        return await real_execute(identifier, tool, current, args)
    monkeypatch.setattr(instance, 'execute', execution)
    instance.semaphore = asyncio.Semaphore(2)
    jobs = [asyncio.create_task(instance.handle(request(project, 'exec', command='fixture', idempotency_key=uuid.uuid4().hex))) for _ in range(2)]
    try:
        await asyncio.wait_for(both.wait(), 1)
        (root / 'file').write_text('readable')
        call = request(project, 'read', path='file')
        await asyncio.wait_for(instance.handle(call), 1)
        assert instance.journal.status(call['id'])['result']['data']['content'] == 'readable'
    finally:
        release.set()
        await asyncio.gather(*jobs)


@pytest.mark.asyncio
@pytest.mark.parametrize('queue', ['project', 'worker', 'resource'])
async def test_accepted_queue_expires_before_lock_release(agent, queue):
    instance, project, root = agent
    (root / 'file').write_text('untouched')
    call = request(project, 'read' if queue == 'resource' else 'fs_read', path='file')
    call['not_after'] = time.time() + .1
    if queue == 'worker':
        # Legacy file reads use the same bounded I/O lane as core read.
        instance.read_semaphore = asyncio.Semaphore(0)
        await asyncio.wait_for(instance.handle(call), 1)
    else:
        async with instance.project_slot(root, write=True, operation_id='holder'):
            await asyncio.wait_for(instance.handle(call), 1)
            assert instance._active_slots
    result = instance.journal.status(call['id'])['result']
    assert not result['ok'] and result['error']['code'] == 'QUEUE_EXPIRED'
    assert not instance.resources.waiting


@pytest.mark.asyncio
async def test_resource_overlap_fairness_cancellation_and_disjoint_paths():
    queue = ResourceQueue()
    events = []
    async def take(label, claims):
        async with queue.slot(label, claims, lambda blockers: None):
            events.append(label)
    async with queue.slot('parent', [Claim('agent', 'path', '/p', True)], lambda blockers: None):
        blocked = asyncio.create_task(take('child', [Claim('agent', 'path', '/p/file', False)]))
        await asyncio.sleep(0)
        await take('other', [Claim('agent', 'path', '/other', True)])
        assert events == ['other']
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)
        assert not queue.waiting
    await take('next', [Claim('agent', 'path', '/p/file', True)])
    assert events[-1] == 'next' and not queue.active


@pytest.mark.asyncio
async def test_facades_enforce_action_scopes_and_process_waits_are_parallel(runtime, monkeypatch):
    instance, principal = runtime
    caller = reader_principal(instance)
    with pytest.raises(DevError) as denied:
        await instance.invoke('computer', {'operation': 'apps', 'project': 'Fixture'}, caller)
    assert denied.value.code == 'WORKSPACE_POLICY_DENIED'
    with pytest.raises(DevError) as denied:
        await instance.invoke('browser', {'operation': 'open', 'project': 'Fixture', 'url': 'https://example.invalid', 'idempotency_key': 'open-browser'}, caller)
    assert denied.value.code == 'WORKSPACE_POLICY_DENIED'
    result = await instance.invoke('workspace', {}, caller)
    assert result['projects'][0]['alias'] == 'Fixture'
    calls = []
    original = instance.invoke
    ready = asyncio.Event()
    async def invoke(name, args, user):
        if name == 'operations_wait':
            calls.append(args['operation_id'])
            if len(calls) == 2: ready.set()
            await asyncio.wait_for(ready.wait(), 1)
            return {'operation_id': args['operation_id'], 'pending': False}
        return await original(name, args, user)
    monkeypatch.setattr(instance, 'invoke', invoke)
    result = await instance.invoke('process', {'operation': 'wait', 'operation_ids': ['one', 'two']}, principal)
    assert len(result['operations']) == 2 and calls == ['one', 'two']


@pytest.mark.asyncio
async def test_saved_vps_target_keeps_credentials_private_and_binding_pinned(runtime):
    instance, principal = runtime
    with observed_runner(instance, 'dev'):
        resource = instance.vps.save(VPSInput(name='Test', runner_device_id='dev', host_alias='production', idempotency_key='fixture-vps-resource'), principal)
    connection = instance.vps.bind(VPSBindingSave(resource_id=resource['id'], name='Test VPS', display_mode='top_level', idempotency_key='fixture-vps-binding'), principal)
    args = {'target': connection['target'], 'command': 'true', 'yield_seconds': 0, 'idempotency_key': 'saved-connection'}
    receipt = await instance.invoke('exec', args, principal)
    row = instance.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    request_data = json.loads(instance.store.decrypt(row['payload']))
    assert request_data['vps_ref']['id'] == connection['id']
    assert row['project_id'] is None  # Whole VPS does not require a fictitious local project.
    assert 'password' not in json.dumps(request_data) + row['args_summary']
    transport = instance.vps.transport(request_data, None)
    assert transport['tool'] == 'exec'
    target = transport['project']['_ssh_target']
    assert target['protocol'] == 2 and target['vps_id'] == connection['id'] and target['resource_id'] == resource['id']
    assert target['connection_ref'] == 'production' and target['host_alias'] == 'production' and len(target['target_fingerprint']) == 64
    assert transport['args'] == request_data['args']
    retry = await instance.invoke('exec', args, principal)
    assert retry['operation_id'] == receipt['operation_id']
    saved=instance.vps.get(connection['id'],principal)
    instance.vps.bind(VPSBindingSave(resource_id=resource['id'], name='Test VPS', enabled=False, expected_version=saved['version'], idempotency_key='disable-vps-binding'), principal, connection['id'])
    assert instance.permission_error(row, principal=principal)
    with pytest.raises(DevError):
        instance.vps.transport(request_data, None)


def test_desktop_native_images_survive_facade_and_batch_poll():
    data = normalize_content(frame())
    direct = core_result('computer', {'operation': 'observe'}, data)
    polled = core_result('process', {'operation': 'get'}, {'operations': [
        {'tool': 'computer_observe', 'state': 'succeeded', 'result': {'ok': True, 'data': data}}]})
    for value in [direct, polled]:
        assert any(block['type'] == 'image' for block in value['content'])
        assert '_computer_content' not in json.dumps(value['structuredContent'])
    expired = {**data, 'computer_expires_at': time.time() - 1}
    assert len(core_result('computer', {'operation': 'observe'}, expired)['content']) == 1


def test_bridge_does_not_invent_lookup_filters_or_workspace_arguments():
    for name in ['workspace', 'process']:
        call = {'method': 'tools/call', 'params': {'name': name, 'arguments': {}}}
        assert prepare_request(call) == call


@pytest.mark.asyncio
async def test_complete_capability_discovery_and_no_unmapped_public_tool(runtime):
    instance, principal = runtime
    assert set(TOOLS) - CORE_TOOLS - QUERY_TOOLS - set(REPLACED_MCP_TOOLS) == {
        'integration_control', 'validations_accept', 'get_profile', 'get_access_context',
        'session', 'get_execution_options', 'project_routes_get'}
    for profile in ('core', 'coding', 'full'):
        assert {t['name'] for t in tool_definitions(profile)} == CORE_TOOLS | QUERY_TOOLS | {
            'get_profile', 'get_access_context', 'session', 'get_execution_options', 'sources_list', 'projects_create'}
    for name, operations in CORE_ACTIONS.items():
        for operation, backend in operations.items():
            help = await instance.invoke('workspace', {'operation': 'help', 'tool': name, 'action': operation}, principal)
            assert help['scope'] == TOOLS[backend].scope
            native_import = name == 'write' and operation == 'import'
            assert help['arguments_location'] == ('top-level' if native_import else 'options')
            assert help['inputSchema']['additionalProperties'] is False
            if native_import:
                assert 'file' in help['inputSchema']['required']
                assert 'path' in help['inputSchema']['properties']['options']['required']
                assert 'file' not in help['inputSchema']['properties']['options']['properties']
            else:
                assert not {'project', 'workspace_id', 'idempotency_key'} & help['inputSchema']['properties'].keys()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,operation,options', [
    ('workspace', 'worktree_create', {'label': 'Isolated worktree'}),
    ('edit', 'checkpoint', {}),
    ('write', 'import', {'path': 'file', 'file': {'file_id': 'x', 'download_url': 'https://example.invalid/file'}}),
    ('process', 'validate', {'command': 'true'}),
    ('read', 'lsp', {'action': 'symbols', 'path': 'file.py'}),
])
async def test_advanced_aliases_do_not_bypass_scopes(runtime, name, operation, options):
    instance, principal = runtime
    reader = reader_principal(instance)
    with pytest.raises(DevError) as denied:
        await instance.invoke(name, {'operation': operation, 'project': 'Fixture',
            'options': options, 'idempotency_key': 'advanced-operation'}, reader)
    assert denied.value.code == 'WORKSPACE_POLICY_DENIED'


@pytest.mark.asyncio
async def test_advanced_options_cannot_override_project_or_smuggle_parameters(runtime):
    instance, principal = runtime
    with pytest.raises(DevError, match='顶层'):
        await instance.invoke('read', {'operation': 'history', 'project': 'Fixture', 'options': {'project': 'Other'}}, principal)
    with pytest.raises(DevError) as error:
        await instance.invoke('read', {'operation': 'history', 'project': 'Fixture', 'options': {'surprise': True}}, principal)
    assert error.value.code == 'INVALID_ARGUMENTS'


def test_help_describes_public_batch_ids_and_skill_options():
    from hub.core_tools import help_result
    from jsonschema import Draft202012Validator
    process = help_result('process', 'wait')['inputSchema']
    Draft202012Validator(process).validate({'operation': 'wait', 'operation_ids': ['original-operation'], 'wait_seconds': 1})
    assert 'operation_id' not in process['properties']
    skills = help_result('workspace', 'skills')['inputSchema']
    Draft202012Validator(skills).validate({'operation': 'skills', 'project': 'Fixture', 'options': {'include_disabled': True}})
    assert 'include_disabled' not in skills['properties']
    with pytest.raises(DevError) as retired:
        help_result('conversations', 'associate')
    assert retired.value.code == 'UNKNOWN_OPERATION'
    assert 'conversations' not in help_result()['tools']
    session = help_result('session', 'open')['inputSchema']
    Draft202012Validator(session).validate({'operation': 'open', 'project': 'Fixture',
        'idempotency_key': 'open-original-session'})
    assert not {'space_id', 'profile_id'} & session['properties'].keys()
    assert not any(op.startswith('workflow_') or op == 'handoff'
        for entry in help_result()['tools'].values() for op in entry['operations'])
