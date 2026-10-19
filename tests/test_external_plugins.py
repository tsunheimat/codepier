"""Real API/IAM/gateway integration with a deterministic external transport."""
import json

import pytest

from tests.external_plugin_support import plugin_env, setup, approve, invoke, call, code, must, rpc, update_policy, create_connection


@pytest.mark.parametrize('mode', ['live', 'fixed'])
def test_project_mapping_executes_without_raw_backend_permission(plugin_env, mode):
    v = setup(plugin_env, mode=mode)
    result = invoke(v)
    assert not result.get('isError'), result
    assert result['structuredContent']['arguments'] == {'project_id': 123, 'pipeline_id': 7}
    assert v['backend'].calls[0]['authorization'] == 'Bearer FIXTURE_BACKEND_CREDENTIAL'
    names = {t['name'] for t in rpc(v['b']['viewer'], v['grant']['token'], 'tools/list', {})['tools']}
    assert 'ext.main.pipeline.get' in names
    assert 'ext.main.pipeline.list' not in names
    assert 'rawgitlab__backend_pipeline' not in names
    assert code(invoke(v, {'project_id': 999, 'pipeline_id': 7}, 'rawgitlab__backend_pipeline')) == 'UNKNOWN_TOOL'
    assert len(v['backend'].calls) == 1
    receipt = call(v['b']['viewer'], v['grant']['token'], 'gateway_call_get', {'call_id': result['_meta']['codepier/callId']})
    assert receipt['structuredContent']['state'] == 'completed'
    assert receipt['structuredContent']['plugin']['project_id'] == 'project-a'


def test_explicit_plugin_consent_is_required_in_addition_to_mcp_consent(plugin_env):
    v = setup(plugin_env, consent=False)
    assert code(invoke(v)) == 'UNKNOWN_TOOL'
    assert not v['backend'].calls
    approve(v)
    assert not invoke(v).get('isError')


@pytest.mark.parametrize('extra', [{'project_id': 999}, {'account_id': 'other'}, {'endpoint': 'http://localhost'}, {'role_id': 'owner'}])
def test_client_cannot_override_trusted_mapping(plugin_env, extra):
    v = setup(plugin_env)
    assert code(invoke(v, {'project': 'project-a', 'pipeline_id': 7, **extra})) == 'PLUGIN_ARGUMENTS_INVALID'
    assert not v['backend'].calls


def test_other_project_and_unbound_project_never_inherit_access(plugin_env):
    v = setup(plugin_env)
    assert code(invoke(v, {'project': 'project-b', 'pipeline_id': 7})) == 'PLUGIN_POLICY_DENIED'
    path = f"/api/spaces/team/external-integrations/{v['integration']['id']}/projects/project-a"
    must(v['b']['owner'].delete(path + '?expected_version=1'))
    assert code(invoke(v)) == 'UNKNOWN_TOOL'
    assert not v['backend'].calls
    assert v['app'].state.store.one('SELECT id FROM external_integrations WHERE id=?', (v['integration']['id'],))
    assert v['app'].state.store.one("SELECT id FROM projects WHERE id='project-a'")
    assert v['app'].state.store.one('SELECT id FROM gateway_accounts WHERE id=?', (v['account']['id'],))


def test_target_changes_require_new_preview_and_consent(plugin_env):
    v = setup(plugin_env)
    path = f"/api/spaces/team/external-integrations/{v['integration']['id']}/projects/project-a"
    must(v['b']['owner'].put(path, json={'expected_version': 1, 'config': {'remote_project_id': 789}}))
    assert code(invoke(v)) == 'UNKNOWN_TOOL'
    approve(v)
    assert invoke(v)['structuredContent']['arguments']['project_id'] == 789


def test_stale_configuration_and_cross_account_consent_are_rejected(plugin_env):
    v = setup(plugin_env)
    assert v['b']['owner'].put('/api/spaces/team/external-integrations/main', json=v['integration_body']).status_code == 409
    assert v['b']['viewer'].put('/api/spaces/team/external-integrations/other', json=v['integration_body']).status_code == 403
    path = '/api/spaces/team/external-integrations/grants/' + v['grant']['connection_id'] + '/consent'
    assert v['b']['other'].get(path).status_code == 404
    assert v['b']['viewer'].post(path, json={'confirmed': True, 'expected_profile_version': v['profile']['version'], 'review_hash': '0' * 64}).status_code == 409


def test_same_request_key_observes_receipt_and_conflicts_do_not_dispatch(plugin_env):
    v = setup(plugin_env)
    first = invoke(v, key='plugin-receipt-key')
    second = invoke(v, key='plugin-receipt-key')
    assert first['_meta']['codepier/callId'] == second['_meta']['codepier/callId']
    assert len(v['backend'].calls) == 1
    assert code(invoke(v, {'project': 'project-a', 'pipeline_id': 8}, key='plugin-receipt-key')) == 'GATEWAY_IDEMPOTENCY_CONFLICT'
    assert len(v['backend'].calls) == 1


def test_unknown_outcome_is_not_automatically_replayed(plugin_env):
    v = setup(plugin_env)
    v['backend'].fail_after_call = True
    assert code(invoke(v, key='uncertain-plugin-key')) == 'GATEWAY_BACKEND_TRANSPORT'
    assert code(invoke(v, key='uncertain-plugin-key')) == 'GATEWAY_ORIGINAL_CALL'
    assert len(v['backend'].calls) == 1
    row = v['app'].state.store.one('SELECT * FROM gateway_calls ORDER BY created DESC LIMIT 1')
    assert row['state'] == 'unknown'
    assert json.loads(row['plugin_context'])['integration_id'] == v['integration']['id']


def test_disabling_integration_during_transport_setup_blocks_send(plugin_env):
    v = setup(plugin_env)
    store = v['app'].state.store
    v['backend'].on_initialize = lambda: store.execute('UPDATE external_integrations SET enabled=0,version=version+1 WHERE id=?', (v['integration']['id'],))
    assert code(invoke(v)) in {'PLUGIN_UNAVAILABLE', 'PLUGIN_CONSENT_REQUIRED'}
    assert not v['backend'].calls
    row = store.one('SELECT state FROM gateway_calls ORDER BY created DESC LIMIT 1')
    assert row['state'] == 'rejected'


def test_revocation_after_dispatch_does_not_publish_result(plugin_env):
    v = setup(plugin_env)
    store = v['app'].state.store
    v['backend'].on_call = lambda: store.execute('DELETE FROM external_plugin_consents WHERE grant_id=?', (v['grant']['connection_id'],))
    assert code(invoke(v)) == 'PLUGIN_CONSENT_REQUIRED'
    assert len(v['backend'].calls) == 1
    assert store.one('SELECT state FROM gateway_calls ORDER BY created DESC LIMIT 1')['state'] == 'unknown'


def test_profile_policy_removal_is_live_without_any_old_role_editor(plugin_env):
    v=setup(plugin_env);profile=v['profile']
    old=v['b']['owner'].put('/api/access-roles/'+profile['id'],json={'label':'Removed role editor'})
    assert old.status_code==404
    assert must(v['b']['owner'].get('/api/spaces/team/profiles/'+profile['id']))['policy']==profile['policy']
    update_policy(v,plugin_rules=[])
    assert code(invoke(v))=='UNKNOWN_TOOL'
    assert not v['backend'].calls


def test_fixed_connection_cannot_gain_new_plugin_actions(plugin_env):
    v = setup(plugin_env, mode='fixed')
    update_policy(v, plugin_rules=[{'integration_id': v['integration']['id'], 'projects': ['project-a', 'project-b'],
        'actions': ['pipeline.get', 'pipeline.list']}])
    preview = approve(v)
    assert {(r['project_id'], r['action']) for r in preview['routes']} == {('project-a', 'pipeline.get')}
    assert code(invoke(v, {'project': 'project-a'}, 'ext.main.pipeline.list')) == 'UNKNOWN_TOOL'
    assert code(invoke(v, {'project': 'project-b', 'pipeline_id': 7})) == 'PLUGIN_POLICY_DENIED'


def test_read_list_uses_constant_page_size_and_shared_integration(plugin_env):
    v = setup(plugin_env, actions=['pipeline.get', 'pipeline.list'], projects=['project-a', 'project-b'])
    result = invoke(v, {'project': 'project-b', 'page': 2}, 'ext.main.pipeline.list')
    assert result['structuredContent']['arguments'] == {'project_id': 456, 'page': 2, 'per_page': 20}
    assert code(invoke(v, {'project': 'project-b', 'per_page': 10000}, 'ext.main.pipeline.list')) == 'PLUGIN_ARGUMENTS_INVALID'
    assert len(v['backend'].calls) == 1


def test_backend_schema_drift_and_private_account_change_fail_closed(plugin_env):
    v = setup(plugin_env)
    store = v['app'].state.store
    row = store.one('SELECT catalog FROM gateway_accounts WHERE id=?', (v['account']['id'],))
    tools = json.loads(row['catalog'])
    selected = next(tool for tool in tools if tool['name'] == 'backend_pipeline')
    selected['description'] = 'changed'
    store.execute('UPDATE gateway_accounts SET catalog=? WHERE id=?', (json.dumps(tools), v['account']['id']))
    assert code(invoke(v)) == 'UNKNOWN_TOOL'
    assert not v['backend'].calls
    store.execute('UPDATE gateway_accounts SET catalog=?,sharing=? WHERE id=?', (row['catalog'], 'private', v['account']['id']))
    assert code(invoke(v)) == 'UNKNOWN_TOOL'
    assert not v['backend'].calls


def test_receipt_cannot_be_read_by_a_different_grant(plugin_env):
    v = setup(plugin_env)
    result = invoke(v)
    new=create_connection(v['b']['viewer'],v['profile'])
    assert code(call(v['b']['viewer'], new['token'], 'gateway_call_get', {'call_id': result['_meta']['codepier/callId']})) == 'GATEWAY_CALL_NOT_FOUND'
