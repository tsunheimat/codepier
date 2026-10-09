"""Real isolated HTTP/SQLite, simulated host identities; never a ChatGPT account."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import uuid

import pytest

from tests.test_audit_api import api as api
from tests.test_conversations import rpc, value
from tests.test_iam_integration import team as team, assign
from tests.test_mcp_gateway import gw as gw, connector, account, publish
from tests.test_resource_access import connection
from tests.test_roles import role, must


def sessions(client, **filters):
    return must(client.get('/api/audit/sessions', params=filters))


def entries(client, **filters):
    return must(client.get('/api/call-log', params=filters))['operations']


@pytest.mark.integration
def test_ten_shared_connection_sessions_overlap_native_and_gateway_on_two_projects(gw):
    app, browsers, backend = gw
    client, store = browsers['alice'], app.state.store
    c = connector(browsers); binding = publish(browsers, account(browsers, c))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,space_id,owner_user_id,created) VALUES('project-two','Second','second','device-team','/tmp/second','write',1,'team','owner',?)", (time.time(),))
    r = role(browsers['owner'], label='worker', project_rules=[{'actions': ['read', 'write', 'execute'], 'all_projects': True}],
             connector_rules=[{'binding_id': binding['id'], 'tools': ['echo', 'run']}])
    assign(browsers['owner'], r, 'alice')
    grant = must(connection(client, r, 'role', confirm_external_mcp=True), 201)
    release, entered = threading.Event(), threading.Event()
    original = backend.handle

    async def held(request):
        if json.loads(request.content)['method'] == 'tools/call':
            entered.set()
            await asyncio.to_thread(release.wait)
        return await original(request)

    # A real transport fixture with an async dispatch barrier, not invented SQL
    # state: every native call returns while its durable queued receipt survives.
    import httpx
    from hub.gateway.remote import RemotePool
    from tests.test_mcp_gateway import public_dns
    old_pool = app.state.gateway.pool
    app.state.gateway.pool = RemotePool(resolver=public_dns, transport=httpx.MockTransport(held))
    # The discovery-only old pool has no ongoing calls.
    client.client.portal.call(old_pool.close)

    def native(index, project):
        response = rpc(client, grant['token'], 'exec', {'project': project, 'command': 'true', 'yield_seconds': 0,
            'idempotency_key': uuid.uuid4().hex}, {'openai/session': f'simulated-tab-{index}'})
        result = value(response)
        assert result['pending']
        return result['operation_id']

    with ThreadPoolExecutor(max_workers=20) as executor:
        gateway_futures = []
        try:
            # Preserve the four-validator admission ceiling: start the next
            # validation after admission, while all downstream calls stay open.
            for i in range(10):
                gateway_futures.append(executor.submit(rpc, client, grant['token'], 'kiln__run', {}, {'openai/session': f'simulated-tab-{i}'}))
                deadline = time.monotonic() + 10
                while store.one("SELECT count(*) AS n FROM gateway_calls WHERE state='running'")['n'] != i + 1:
                    assert time.monotonic() < deadline, 'gateway admission did not occur'
                    time.sleep(0.01)
            assert entered.wait(10)
            first = list(executor.map(lambda i: native(i, 'same-alias'), range(10)))
            second = list(executor.map(lambda i: native(i, 'Second'), range(10)))
            observed = sessions(client)
            assert len(observed['sessions']) == 10
            assert {s['grant_id'] for s in observed['sessions']} == {grant['grant_id']}
            identifiers = {s['id'] for s in observed['sessions']}
            assert len(identifiers) == 10
            for s in observed['sessions']:
                assert s['active_count'] == 3 and s['state'] == 'active', s
                assert len(s['current']) == 3
                assert {o['kind'] for o in s['current']} == {'native', 'mcp'}
                assert {ref['id'] for o in s['current'] for ref in o['resources']} == {'project-team', 'project-two', binding['id']}
                assert all(o['state'] in {'queued', 'running'} for o in s['current'])
                assert all({ref['id'] for ref in o['sessions']} == {s['id']} for o in s['current'])
                assert len(entries(client, session=s['id'])) == 3
            assert sessions(browsers['owner'])['sessions'] == []
            assert sessions(browsers['bob'])['sessions'] == []
            assert sessions(browsers['legacy'])['sessions'] == []
            # First fixture user is the instance admin: existing native Audit
            # visibility remains, but it never reveals another user's sessions
            # or private downstream gateway responses.
            admin_rows = entries(browsers['owner'])
            assert len(admin_rows) == 20 and all(r['kind'] == 'native' and not r['sessions'] for r in admin_rows)
            assert not entries(browsers['bob'])
        finally:
            release.set()
        responses = [must(f.result()) for f in gateway_futures]
    assert all('error' not in r and not r['result'].get('isError') for r in responses)
    assert backend.effects == 10
    # The fixture has no Agent. Drive persisted receipts through terminal states
    # explicitly; do not confuse the already-returned HTTP calls with completion.
    store.execute("UPDATE operations SET state='succeeded',updated=? WHERE id IN (" + ','.join('?' for _ in first) + ')', (time.time(), *first))
    for s in sessions(client)['sessions']:
        assert s['active_count'] == 1
        assert len(s['current']) == 1
        assert [r['id'] for r in s['current'][0]['resources']] == ['project-two']
    assert sessions(client, project='project-team')['sessions'] == []
    assert len(sessions(client, project='project-two')['sessions']) == 10
    store.execute("UPDATE operations SET state='failed',error='fixture failure',updated=? WHERE id IN (" + ','.join('?' for _ in second) + ')', (time.time(), *second))
    assert sessions(client, state='active')['sessions'] == []
    assert len(sessions(client, state='failed')['sessions']) == 10
    for s in sessions(client)['sessions']:
        assert s['state'] == 'failed' and s['current'][0]['state'] == 'failed'
    assert app.state.runtime.session_activity.write_errors == 0
    assert app.state.runtime.conversations.write_errors == 0


@pytest.mark.parametrize('metadata,expected', [(None, 'missing'), ({}, 'missing'), ({'openai/session': 42}, 'invalid'),
    ({'openai/session': ''}, 'invalid'), ({'session_id': 'do-not-substitute'}, 'unsupported')])
def test_immediate_calls_and_metadata_gaps_are_visible_without_registration(api, metadata, expected):
    app, client, token = api
    response = rpc(client, token, metadata=metadata)
    assert value(response)['projects']
    diagnostic = response.json()['result']['_meta']['codepier/activity']
    assert diagnostic['session_id'] is None and diagnostic['correlation'] == expected
    rows = entries(client, correlation='unassociated')
    assert len(rows) == 1 and rows[0]['id'] == diagnostic['id']
    assert rows[0]['state'] == 'returned' and rows[0]['correlation'] == expected
    assert rows[0]['tool'] == 'project_query' and rows[0]['action'] == 'list'
    assert not sessions(client)['sessions'] and sessions(client)['unassociated_count'] == 1
    assert not app.state.store.all('SELECT * FROM conversations')


def test_errors_before_resolution_and_authorization_reads_and_resources_are_observed(api):
    app, client, token = api
    meta = {'openai/session': 'errors-and-reads'}
    good = rpc(client, token, metadata=meta)
    identifier = good.json()['result']['_meta']['codepier/activity']['session_id']
    unknown = rpc(client, token, 'not_a_tool', {'password': 'DO_NOT_STORE'}, meta)
    denied = rpc(client, token, 'exec', {'project': 'fixture', 'command': 'secret chat content', 'yield_seconds': 0}, meta)
    assert unknown.json()['result']['isError'] and denied.json()['result']['isError']
    response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 9, 'method': 'resources/read', 'params': {'uri': 'rd://projects', '_meta': meta}},
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'})
    assert response.status_code == 200
    rows = entries(client, session=identifier)
    assert len(rows) == 4
    assert {r['tool'] for r in rows if r['state'] == 'failed'} == {'not_a_tool', 'exec'}
    assert rows[0]['tool'] == 'resources/read' and rows[0]['state'] == 'returned'
    assert 'DO_NOT_STORE' not in json.dumps(app.state.store.all('SELECT * FROM audit_activity'))
    assert 'secret chat content' not in json.dumps(app.state.store.all('SELECT * FROM audit_activity'))
    details = must(client.get('/api/call-log/' + rows[0]['id']))
    assert details['request_id'] and details['sessions'][0]['id'] == identifier
    before = app.state.store.one('SELECT count(*) AS n FROM audit_activity')['n']
    sessions(client); entries(client); client.get('/api/call-log/' + rows[0]['id'])
    assert app.state.store.one('SELECT count(*) AS n FROM audit_activity')['n'] == before


def test_gateway_return_is_not_downstream_job_completion_and_private_result_is_redacted(gw):
    app, b, backend = gw
    c = connector(b); binding = publish(b, account(b, c))
    r = role(b['owner'], label='worker', project_rules=[], connector_rules=[{'binding_id': binding['id'], 'tools': ['run']}])
    assign(b['owner'], r, 'alice')
    g = must(connection(b['alice'], r, 'role', confirm_external_mcp=True), 201)
    backend.result_override = {'content': [{'type': 'text', 'text': 'job submitted'}],
        'structuredContent': {'job_id': 'fixture-job', 'state': 'running', 'password': 'HIDDEN_DOWNSTREAM_SECRET'}}
    response = rpc(b['alice'], g['token'], 'kiln__run', {}, {'openai/session': 'async-job'})
    assert not response.json()['result'].get('isError')
    row = sessions(b['alice'])['sessions'][0]
    assert row['active_count'] == 0 and row['state'] == 'recent'
    assert row['current'][0]['state'] == 'completed'
    assert '下游异步任务' in row['current'][0]['state_note']
    identifier = row['current'][0]['id']
    detail = b['alice'].get('/api/call-log/' + identifier)
    assert detail.status_code == 200, detail.text
    assert 'HIDDEN_DOWNSTREAM_SECRET' not in detail.text and 'fixture-job' in detail.text
    assert b['owner'].get('/api/call-log/' + identifier).status_code == 404
    assert b['bob'].get('/api/call-log/' + identifier).status_code == 404
    assert b['owner'].get('/api/call-log', params={'session': row['id']}).status_code == 404
    app.state.gateway.enabled = False
    assert b['alice'].get('/api/call-log/' + identifier).status_code == 403


def test_gateway_arguments_do_not_impersonate_native_resource_references(gw):
    app, b, backend = gw
    binding = publish(b, account(b, connector(b)))
    r = role(b['owner'], label='reader', project_rules=[{'actions': ['read'], 'projects': ['project-team']}],
             connector_rules=[{'binding_id': binding['id'], 'tools': ['run']}])
    assign(b['owner'], r, 'alice')
    g = must(connection(b['alice'], r, 'role', confirm_external_mcp=True), 201)
    response = rpc(b['alice'], g['token'], 'kiln__run', {'project': 'same-alias'}, {'openai/session': 'external-target'})
    assert response.json()['result']['isError']  # Strict downstream schema rejects this argument.
    assert backend.effects == 0
    observed = sessions(b['alice'])['sessions'][0]['current'][0]
    assert observed['state'] == 'failed'
    assert observed['resources'] == [{'type': 'mcp', 'id': binding['id'], 'name': 'Fixture / kiln'}]
    assert not app.state.store.all('SELECT * FROM operations')


def test_same_host_identifier_does_not_cross_grants_or_space_and_cannot_change_permissions(team):
    app, b = team
    r = role(b['owner'], label='reader', project_rules=[{'actions': ['read'], 'projects': ['project-team']}])
    for user in ('alice', 'bob'): assign(b['owner'], r, user)
    grants = [(b['alice'], must(connection(b['alice'], r, 'role'), 201)),
              (b['alice'], must(connection(b['alice'], r, 'role'), 201)),
              (b['bob'], must(connection(b['bob'], r, 'role'), 201))]
    identities = []
    for client, grant in grants:
        response = rpc(client, grant['token'], metadata={'openai/session': 'same-host-value'})
        value(response)
        identities.append(response.json()['result']['_meta']['codepier/activity']['session_id'])
    assert len(set(identities)) == 3
    assert {s['id'] for s in sessions(b['alice'])['sessions']} == set(identities[:2])
    assert {s['id'] for s in sessions(b['bob'])['sessions']} == {identities[2]}
    denied = rpc(b['alice'], grants[0][1]['token'], 'exec', {'project': 'same-alias', 'command': 'true', 'yield_seconds': 0},
        {'openai/session': 'same-host-value', 'codepier/conversation': {'role_id': 'admin', 'space_id': 'legacy'}})
    assert denied.json()['result']['isError']
    assert not app.state.store.all('SELECT * FROM operations')
    for user in ('owner', 'bob'):
        assert b[user].get('/api/call-log', params={'session': identities[0]}).status_code == 404
    assert not sessions(b['legacy'])['sessions']


def test_failed_optional_activity_write_does_not_discard_native_receipt(api, monkeypatch):
    app, client, _ = api
    app.state.store.execute('UPDATE projects SET allow_tasks=1')
    r = role(client, label='worker', project_rules=[{'actions': ['read', 'execute'], 'projects': ['project']}])
    g = must(connection(client, r, 'role'), 201)
    def broken(*args, **kwargs):
        raise RuntimeError('private diagnostic')
    monkeypatch.setattr(app.state.runtime.session_activity, 'begin', broken)
    result = value(rpc(client, g['token'], 'exec', {'project': 'fixture', 'command': 'true', 'yield_seconds': 0,
        'idempotency_key': uuid.uuid4().hex}, {'openai/session': 'cannot-record'}))
    assert result['pending'] and result['operation_id']
    assert len(entries(client)) == 1 and entries(client)[0]['correlation'] == 'unassociated'
    assert app.state.runtime.session_activity.write_errors == 1
    assert sessions(client)['write_errors'] == 1


def test_completed_idempotent_replays_keep_receipt_and_link_each_observation(api):
    app, client, _ = api
    app.state.store.execute('UPDATE projects SET allow_tasks=1')
    r = role(client, label='worker', project_rules=[{'actions': ['read', 'execute'], 'projects': ['project']}])
    g = must(connection(client, r, 'role'), 201)
    args = {'project': 'fixture', 'command': 'true', 'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex}
    first = rpc(client, g['token'], 'exec', args, {'openai/session': 'original'})
    operation = value(first)['operation_id']
    app.state.store.execute("UPDATE operations SET state='succeeded',result=?,updated=? WHERE id=?",
                            (json.dumps({'ok': True, 'data': {'exit_code': 0}}), time.time(), operation))
    for host_id in ('original', 'second'):
        response = rpc(client, g['token'], 'exec', args, {'openai/session': host_id})
        assert value(response)['operation_id'] == operation
        diagnostic = response.json()['result']['_meta']['codepier/activity']
        observation = must(client.get('/api/call-log/' + diagnostic['id']))
        assert observation['state'] == 'returned'
        assert observation['receipts'][0]['id'] == operation
    rows = sessions(client)['sessions']
    assert len(rows) == 2 and all(s['current'][0]['id'] == operation for s in rows)
    assert app.state.store.one('SELECT count(*) AS n FROM operations')['n'] == 1
    detail = must(client.get('/api/call-log/' + operation))
    assert len(detail['observations']) == 3


def test_valid_host_identity_survives_bad_optional_title_and_secrets_are_not_metadata(api):
    app, client, token = api
    response = rpc(client, token, metadata={'openai/session': 'real-opaque-id', 'codepier/conversation': {'label': 'x' * 1000}})
    value(response)
    assert response.json()['result']['_meta']['codepier/activity']['session_id']
    response = rpc(client, token, metadata={'openai/session': 'Bearer SECRET_TOKEN_VALUE', 'chat': 'PRIVATE_CHAT_TEXT'})
    value(response)
    assert response.json()['result']['_meta']['codepier/activity']['correlation'] == 'invalid'
    stored = json.dumps(app.state.store.all('SELECT * FROM conversations') + app.state.store.all('SELECT * FROM audit_activity'))
    assert 'SECRET_TOKEN_VALUE' not in stored and 'PRIVATE_CHAT_TEXT' not in stored


def test_restart_preserves_completed_history_and_marks_unfinished_call_unknown(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from hub.app import create_app
    from hub.principal import Principal
    from tests.legacy_iam_fixture import seed_owner
    monkeypatch.setenv('HUB_PUBLIC_URL', 'http://testserver')
    monkeypatch.setenv('MCP_PUBLIC_URL', '')
    directory = tmp_path / 'hub'
    app = create_app(str(directory))
    seed_owner(app.state.store, 'owner', 'admin')
    grant = app.state.auth.issue_grant(Principal('panel:admin', 'owner', {'read'}, ['*'], admin=True, space_id='legacy'), 'fixture', ['read'], ['*'])
    with TestClient(app) as client:
        response = rpc(client, grant['token'], metadata={'openai/session': 'finished-read'})
        value(response)
        complete = app.state.store.one('SELECT * FROM audit_activity')
        principal = app.state.runtime.grant_principal(app.state.store.one('SELECT * FROM grants WHERE id=?', (grant['grant_id'],)))
        # Persist the same admission checkpoint a process crash can leave behind.
        interrupted = app.state.runtime.session_activity.begin(principal, 'tools/call', {'name': 'project_query'}, {'openai/session': 'lost-request'})
    restored = create_app(str(directory))
    with TestClient(restored):
        store = restored.state.store
        assert store.one('SELECT * FROM audit_activity WHERE id=?', (complete['id'],)) == complete
        lost = store.one('SELECT * FROM audit_activity WHERE id=?', (interrupted['id'],))
        assert lost['state'] == 'unknown' and lost['error_code'] == 'HUB_RESTARTED'
        principal = restored.state.runtime.grant_principal(store.one('SELECT * FROM grants WHERE id=?', (grant['grant_id'],)))
        result = restored.state.runtime.session_activity.sessions(principal)
        row = next(s for s in result['sessions'] if s['id'] == interrupted['session_id'])
        assert row['active_count'] == 0 and row['attention_count'] == 1 and row['state'] == 'attention'
        assert not store.all('SELECT * FROM operations')
        assert not store.all('PRAGMA foreign_key_check')
