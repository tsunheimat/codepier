"""Project-set and space-scope acceptance over the actual permission pipeline."""
import copy

import pytest

from hub.external_plugins.contracts import Registry
from tests.external_plugin_support import plugin_env, setup, approve, invoke, code, must, rpc, update_policy, create_connection
from tests.current_model import CurrentModel


def test_fixed_connection_retains_large_explicit_project_set(plugin_env):
    value = setup(plugin_env)
    store = value['app'].state.store
    projects = [f'plugin-project-{i:02d}' for i in range(40)]
    model=CurrentModel(store)
    for project in projects:
        model.project(project,space='team',folder='plugin-folder',owner='owner',subpath=project)
    for i, project in enumerate(projects):
        must(value['b']['owner'].put(
            f"/api/spaces/team/external-integrations/{value['integration']['id']}/projects/{project}",
            json={'expected_version': 0, 'config': {'remote_project_id': 1000 + i}}))
    profile = update_policy(value,
        project_rules=[{'projects': projects + ['project-b'], 'actions': ['read']}],
        plugin_rules=[{'integration_id': value['integration']['id'], 'projects': projects,
                       'actions': ['pipeline.get']}])
    value['grant']=create_connection(value['b']['viewer'],profile,mode='fixed')
    preview = approve(value)
    assert {r['project_id'] for r in preview['routes']} == set(projects)
    result = invoke(value, {'project': projects[-1], 'pipeline_id': 7})
    assert result['structuredContent']['arguments'] == {'project_id': 1039, 'pipeline_id': 7}

    # A live Profile edit cannot extend the fixed Connection to another target.
    update_policy(value, plugin_rules=[{'integration_id': value['integration']['id'],
        'projects': projects[1:] + ['project-b'], 'actions': ['pipeline.get', 'pipeline.list']}])
    preview = approve(value)
    assert {(r['project_id'], r['action']) for r in preview['routes']} == {
        (project, 'pipeline.get') for project in projects[1:]}
    assert code(invoke(value, {'project': projects[0], 'pipeline_id': 7})) == 'PLUGIN_POLICY_DENIED'
    assert code(invoke(value, {'project': 'project-b', 'pipeline_id': 7})) == 'PLUGIN_POLICY_DENIED'
    assert code(invoke(value, {'project': projects[1]}, 'ext.main.pipeline.list')) == 'UNKNOWN_TOOL'
    assert not invoke(value, {'project': projects[1], 'pipeline_id': 7}).get('isError')


@pytest.mark.parametrize('mode', ['live', 'fixed'])
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
    must(value['b']['owner'].put('/api/spaces/team/external-integrations/main', json={
        **value['integration_body'], 'expected_version': 1,
        'manifest_hash': registry.catalog()[0]['manifest_hash'],
        'config': {'remote_project_id': 900}}))
    profile = update_policy(value, project_rules=[], plugin_rules=[{
        'integration_id': value['integration']['id'], 'scope': 'space',
        'projects': [], 'actions': ['pipeline.shared']}])
    value['grant']=create_connection(value['b']['viewer'],profile,mode=mode)
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
