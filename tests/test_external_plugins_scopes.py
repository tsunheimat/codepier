"""Project-set and space-scope acceptance over the actual permission pipeline."""
import copy

import pytest

from hub.external_plugins.contracts import Registry
from tests.external_plugin_support import plugin_env, setup, approve, invoke, code, must, rpc, update_role


def test_fixed_connection_retains_large_explicit_project_set(plugin_env):
    value = setup(plugin_env)
    store = value['app'].state.store
    projects = [f'plugin-project-{i:02d}' for i in range(40)]
    with store.transaction():
        for project in projects:
            store.db.execute('''INSERT INTO projects
                (id,alias,alias_key,device_id,root,description,mode,allow_tasks,created,space_id,owner_user_id)
                SELECT ?,?,?,device_id,root,description,mode,allow_tasks,created,space_id,owner_user_id
                FROM projects WHERE id='project-a' ''', (project, project, project))
    for i, project in enumerate(projects):
        must(value['b']['owner'].put(
            f"/api/external-integrations/{value['integration']['id']}/projects/{project}",
            json={'expected_version': 0, 'config': {'remote_project_id': 1000 + i}}))
    role = update_role(value,
        project_rules=[{'projects': projects + ['project-b'], 'actions': ['read']}],
        plugin_rules=[{'integration_id': value['integration']['id'], 'projects': projects,
                       'actions': ['pipeline.get']}])
    value['grant'] = must(value['b']['viewer'].post('/api/client-connections', json={
        'name': 'Large fixed plugin client', 'role_id': role['id'],
        'expected_role_version': role['version'], 'authorization_mode': 'fixed',
        'confirm_external_mcp': True, 'idempotency_key': 'large-fixed-plugin-scope'}), 201)
    preview = approve(value)
    assert {r['project_id'] for r in preview['routes']} == set(projects)
    result = invoke(value, {'project': projects[-1], 'pipeline_id': 7})
    assert result['structuredContent']['arguments'] == {'project_id': 1039, 'pipeline_id': 7}

    # A dynamic role change cannot extend this fixed connection to another target.
    update_role(value, plugin_rules=[{'integration_id': value['integration']['id'],
        'projects': projects[1:] + ['project-b'], 'actions': ['pipeline.get', 'pipeline.list']}])
    preview = approve(value)
    assert {(r['project_id'], r['action']) for r in preview['routes']} == {
        (project, 'pipeline.get') for project in projects[1:]}
    assert code(invoke(value, {'project': projects[0], 'pipeline_id': 7})) == 'PLUGIN_POLICY_DENIED'
    assert code(invoke(value, {'project': 'project-b', 'pipeline_id': 7})) == 'PLUGIN_POLICY_DENIED'
    assert code(invoke(value, {'project': projects[1]}, 'ext.main.pipeline.list')) == 'UNKNOWN_TOOL'
    assert not invoke(value, {'project': projects[1], 'pipeline_id': 7}).get('isError')


@pytest.mark.parametrize('mode', ['role', 'fixed'])
def test_explicit_space_action_uses_space_target_without_project_authority(plugin_env, mode):
    value = setup(plugin_env)
    manifest = Registry.bundled().catalog()[0]['manifest']
    manifest['version'] = '0.2.0'
    manifest['space_config_schema'] = copy.deepcopy(manifest['project_config_schema'])
    action = copy.deepcopy(manifest['actions']['pipeline.get'])
    action['scope'] = 'space'
    action['call']['arguments']['project_id'] = {'from_space': 'remote_project_id'}
    manifest['actions']['pipeline.shared'] = action
    registry = Registry([manifest])
    value['app'].state.gateway.plugins.registry = registry
    must(value['b']['owner'].put('/api/external-integrations/main', json={
        **value['integration_body'], 'expected_version': 1,
        'manifest_hash': registry.catalog()[0]['manifest_hash'],
        'config': {'remote_project_id': 900}}))
    role = update_role(value, project_rules=[], plugin_rules=[{
        'integration_id': value['integration']['id'], 'scope': 'space',
        'projects': [], 'actions': ['pipeline.shared']}])
    value['grant'] = must(value['b']['viewer'].post('/api/client-connections', json={
        'name': 'Space-only client', 'role_id': role['id'],
        'expected_role_version': role['version'], 'authorization_mode': mode,
        'confirm_dynamic_role': mode == 'role', 'confirm_external_mcp': True,
        'idempotency_key': 'space-only-plugin-' + mode}), 201)
    preview = approve(value)
    assert len(preview['routes']) == 1
    assert preview['routes'][0]['project_id'] == ''
    assert preview['routes'][0]['space_config'] == {'remote_project_id': 900}
    result = invoke(value, {'pipeline_id': 8}, 'ext.main.pipeline.shared')
    assert result['structuredContent']['arguments'] == {'project_id': 900, 'pipeline_id': 8}
    tools = rpc(value['b']['viewer'], value['grant']['token'], 'tools/list', {})['tools']
    definition = next(t for t in tools if t['name'] == 'ext.main.pipeline.shared')
    assert 'project' not in definition['inputSchema']['properties']
    assert code(invoke(value, {'project': 'project-a', 'pipeline_id': 8},
                       'ext.main.pipeline.shared')) == 'PLUGIN_ARGUMENTS_INVALID'
    assert code(invoke(value, {'pipeline_id': 8, 'project_id': 123},
                       'ext.main.pipeline.shared')) == 'PLUGIN_ARGUMENTS_INVALID'
    assert len(value['backend'].calls) == 1
