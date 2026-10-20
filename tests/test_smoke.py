"""Golden paths against a real isolated Hub, real Agent, and real local filesystem."""
from __future__ import annotations
import uuid
import httpx
import pytest

pytestmark = pytest.mark.smoke


@pytest.fixture(scope='module', autouse=True)
def shell_enabled(stack):
    from agent.shell import default_command
    from shared.util import atomic_json
    stack.stop_agent()
    stack.config['shell'] = {'enabled': True, 'projects': ['Imago'], 'command': default_command()}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()


def result(stack, name, args):
    if name == 'exec':
        args = {'yield_seconds':0, 'idempotency_key':uuid.uuid4().hex, **args}
    reply = stack.mcp(name, args)
    assert not reply.get('isError'), reply
    data = reply['structuredContent']
    if data.get('pending'):
        operation = stack.poll(data['operation_id'], timeout=30)
        assert operation['state'] == 'succeeded', operation
        return operation['result']['data']
    return data


@pytest.mark.parametrize('path,key', [('/healthz','status'),('/api/devices','devices'),('/api/projects','projects'),('/api/spaces/{space}/connections','connections')])
def test_authenticated_management_snapshot(stack, path, key):
    response = stack.client.get(path.format(space=stack.space_id))
    assert response.status_code == 200 and key in response.json()
    if path == '/api/devices':
        assert any(device['id'] == stack.device and device['online'] for device in response.json()['devices'])


@pytest.mark.parametrize('name,args,key', [
    ('read', {'path':'README.md'}, 'content'),
    ('workspace', {'operation':'tree'}, 'entries'),
    ('exec', {'command':'git status --short'}, 'output'),
    ('exec', {'command':'git log -1 --oneline'}, 'output'),
    ('workspace', {'operation':'status'}, 'shell'),
    ('workspace', {'operation':'tasks'}, 'tasks'),
    ('exec', {'command':'git grep -n greeting -- src'}, 'output'),
])
def test_mcp_read_paths_reach_the_agent(stack, name, args, key):
    data = result(stack, name, {'project':'Imago', **args})
    assert key in data, data


@pytest.mark.parametrize('path', ['/api/projects', '/api/devices'])
def test_anonymous_requests_never_access_private_management(stack, path):
    with httpx.Client(base_url=stack.url, trust_env=False) as client:
        assert client.get(path).status_code == 401


@pytest.mark.parametrize('origin,csrf', [(None, 'incorrect'), ('https://other.invalid', None)])
def test_forged_management_write_is_denied(stack, origin, csrf):
    headers = {'X-RD-CSRF': csrf or stack.client.headers['X-RD-CSRF']}
    if origin: headers['Origin'] = origin
    body = {'alias':'NoAdmission','source_id':stack.folder['id'],'subpath':'.','idempotency_key':uuid.uuid4().hex}
    response = stack.client.post(f'/api/spaces/{stack.space_id}/projects', json=body, headers=headers)
    assert response.status_code == 403


def test_write_is_idempotent_and_audited(stack):
    name = 'smoke-' + uuid.uuid4().hex + '.txt'
    args = {'project':'Imago','path':name,'content':'one durable write','expected_sha256':'new','idempotency_key':uuid.uuid4().hex}
    first = result(stack, 'write', args)
    second = result(stack, 'write', args)
    assert first == second
    assert (stack.imago / name).read_text() == 'one durable write'
    response = stack.client.get('/api/audit')
    assert response.status_code == 200 and 'write' in response.text


def test_project_creation_keeps_explicit_read_boundary(stack):
    directory = stack.root / ('Smoke-' + uuid.uuid4().hex)
    directory.mkdir()
    folder = stack.create_folder({'name':'Read-only smoke',
        'device_id':stack.device,'device_path':str(directory),'mode':'read','allow_tasks':False})
    source = stack.create_source({'name':'Read-only smoke','source_kind':'folder','folder_resource_id':folder['id']})
    project = stack.create_project({'alias':directory.name,'source_id':source['id'],'subpath':'.'})
    view = stack.must(stack.client.get('/api/projects/'+project['id']))
    assert view['mode'] == 'read' and not view['allow_tasks']
    # The project cannot override the folder's read-only setting even with a
    # Profile which otherwise allows write. Validate the real dispatch path.
    profile = stack.create_profile({'project_rules':[{'projects':[project['id']],'actions':['read','write']}]})
    connection = stack.create_connection(profile)
    denied = stack.mcp('write', {'project':project['id'],'path':'forbidden','content':'no',
        'expected_sha256':'new','idempotency_key':uuid.uuid4().hex},token_value=connection['token'])
    assert denied['isError'] and not (directory/'forbidden').exists()
    stack.must(stack.client.delete('/api/projects/' + project['id'],params={'expected_version':project['version']}))


def test_named_task_executes_once_and_returns_real_output(stack):
    data = result(stack, 'exec', {'project':'Imago','task':'smoke','idempotency_key':uuid.uuid4().hex})
    assert data['exit_code'] == 0 and 'PASS: local task completed' in data['output']


def test_secret_path_is_denied(stack):
    reply = stack.mcp('read', {'project':'Imago','path':'.env'})
    assert reply['isError'] and 'never-return-this' not in str(reply)


def test_revoked_token_cannot_invoke_a_tool(stack):
    profile = stack.create_profile({'project_rules':[{'projects':[stack.project['id']],'actions':['read']}]})
    created = stack.create_connection(profile)
    stack.must(stack.client.post(f'/api/spaces/{stack.space_id}/connections/'+created['connection_id']+'/revoke'))
    response = stack.rpc('tools/call', {'name':'workspace','arguments':{'operation':'tree','project':'Imago'}}, token_value=created['token'])
    assert response.status_code == 401
