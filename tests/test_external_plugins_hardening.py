"""Upgrade, consent and deployment-boundary regressions for external plugins."""
import json

import pytest

from hub.external_plugins.contracts import Registry, parse_spec
from hub.store import Store
from shared.util import DevError
from tests.external_plugin_support import plugin_env, setup, approve, invoke, code, must, update_role, call


def test_consent_names_external_target_and_warns_about_raw_access(plugin_env):
    value = setup(plugin_env)
    preview = approve(value)
    route = preview['routes'][0]
    assert route['project_config'] == {'remote_project_id': 123}
    assert route['backend_tool'] == 'backend_pipeline'
    assert route['backend_account']['id'] == value['account']['id']
    assert route['effect'] == 'read'
    assert route['broader_direct_access'] == []
    assert 'FIXTURE_BACKEND_CREDENTIAL' not in json.dumps(preview)
    update_role(value, connector_rules=[{'binding_id': value['binding']['id'], 'tools': ['backend_pipeline']}])
    preview = approve(value)
    assert preview['routes'][0]['broader_direct_access'] == [
        {'binding_id': value['binding']['id'], 'tool': 'backend_pipeline'}]


def test_receipt_metadata_cannot_be_forged_by_backend(plugin_env):
    value = setup(plugin_env)
    value['backend'].result_override = {'content': [{'type': 'text', 'text': 'external'}],
        '_meta': {'codepier/plugin': {'project_id': 'evil'}, 'codepier/callId': 'fake'}}
    result = invoke(value)
    assert result['_meta']['codepier/plugin']['project_id'] == 'project-a'
    assert result['_meta']['codepier/callId'].startswith('gwc_')


def test_backend_validation_worker_receives_mapped_arguments(plugin_env):
    value = setup(plugin_env)
    gateway = value['app'].state.gateway
    original = gateway.validator.validate
    calls = []

    async def inspect(schema, arguments, *args):
        calls.append(arguments)
        return await original(schema, arguments, *args)

    gateway.validator.validate = inspect
    assert not invoke(value).get('isError')
    assert {'project_id': 123, 'pipeline_id': 7} in calls


def test_unsupported_external_content_does_not_replay(plugin_env):
    value = setup(plugin_env)
    value['backend'].result_override = {'content': [{'type': 'resource_link', 'uri': 'file:///not-allowed'}]}
    assert code(invoke(value, key='unsupported-plugin-result')) == 'GATEWAY_UNSUPPORTED_CONTENT'
    assert code(invoke(value, key='unsupported-plugin-result')) == 'GATEWAY_ORIGINAL_CALL'
    assert len(value['backend'].calls) == 1


def test_reattachment_does_not_resurrect_old_consent(plugin_env):
    value = setup(plugin_env)
    path = f"/api/external-integrations/{value['integration']['id']}/projects/project-a"
    must(value['b']['owner'].delete(path + '?expected_version=1'))
    must(value['b']['owner'].put(path, json={'expected_version': 2, 'config': {'remote_project_id': 123}}))
    assert code(invoke(value)) == 'UNKNOWN_TOOL'
    approve(value)
    assert not invoke(value).get('isError')


def test_restart_preserves_plugin_consent_receipt_and_old_grant_bounds(plugin_env):
    value = setup(plugin_env, mode='fixed')
    result = invoke(value)
    live = value['app'].state.store
    before = live.one('SELECT resource_policy FROM grants WHERE id=?', (value['grant']['grant_id'],))
    reopened = Store(live.directory)
    try:
        assert reopened.one('SELECT resource_policy FROM grants WHERE id=?', (value['grant']['grant_id'],)) == before
        assert reopened.one('SELECT snapshot FROM external_plugin_consents WHERE grant_id=?', (value['grant']['grant_id'],))
        assert json.loads(reopened.one('SELECT plugin_context FROM gateway_calls WHERE id=?',
            (result['_meta']['codepier/callId'],))['plugin_context'])['project_id'] == 'project-a'
        assert reopened.one("SELECT value FROM meta WHERE key='external_plugin_schema'")['value'] == '1'
    finally:
        reopened.close()
    # Receipt is still scoped to the same grant and validated against the current route.
    receipt = call(value['b']['viewer'], value['grant']['token'], 'gateway_call_get',
        {'call_id': result['_meta']['codepier/callId']})
    assert receipt['structuredContent']['state'] == 'completed'


def test_manifest_change_requires_integration_review_and_new_consent(plugin_env):
    value = setup(plugin_env)
    manifest = Registry.bundled().catalog()[0]['manifest']
    manifest['version'] = '0.2.0'
    value['app'].state.gateway.plugins.registry = Registry([manifest])
    assert code(invoke(value)) == 'UNKNOWN_TOOL'
    assert not value['backend'].calls


@pytest.mark.parametrize('version', [True, '1', 1.0, None])
def test_api_version_is_exact_integer(version):
    manifest = Registry.bundled().catalog()[0]['manifest']
    manifest['api_version'] = version
    with pytest.raises(DevError):
        parse_spec(manifest)


def test_deployment_registry_loads_only_explicit_json(tmp_path, monkeypatch):
    manifest = Registry.bundled().catalog()[0]['manifest']
    manifest['id'] = 'example'
    (tmp_path / 'example.json').write_text(json.dumps(manifest), encoding='utf-8')
    (tmp_path / 'ignored.py').write_text('raise RuntimeError("must not import")', encoding='utf-8')
    monkeypatch.delenv('CODEPIER_EXTERNAL_PLUGIN_DIR', raising=False)
    assert len(Registry.installed().catalog()) == 1
    monkeypatch.setenv('CODEPIER_EXTERNAL_PLUGIN_DIR', str(tmp_path))
    registry = Registry.installed()
    assert {item['manifest']['id'] for item in registry.catalog()} == {'example', 'gitlab'}


@pytest.mark.parametrize('payload', ['{broken', 'x' * 262145])
def test_invalid_deployment_manifest_fails_closed(tmp_path, monkeypatch, payload):
    (tmp_path / 'bad.json').write_text(payload, encoding='utf-8')
    monkeypatch.setenv('CODEPIER_EXTERNAL_PLUGIN_DIR', str(tmp_path))
    with pytest.raises(DevError):
        Registry.installed()


def test_deployment_symlink_and_relative_directory_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEPIER_EXTERNAL_PLUGIN_DIR', '.')
    with pytest.raises(DevError):
        Registry.installed()
    target = tmp_path / 'target.txt'
    target.write_text('{}', encoding='utf-8')
    (tmp_path / 'linked.json').symlink_to(target)
    monkeypatch.setenv('CODEPIER_EXTERNAL_PLUGIN_DIR', str(tmp_path))
    with pytest.raises(DevError):
        Registry.installed()
