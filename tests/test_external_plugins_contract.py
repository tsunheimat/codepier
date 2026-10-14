"""Offline contract tests: no network or credentials are needed by authors."""
import copy
import json

import pytest
from pydantic import ValidationError

from hub.external_plugins.contracts import Registry, parse_spec, map_arguments, public_definition
from hub.external_plugins.rules import PluginRule, intersect, allows
from hub.roles import RolePolicy
from shared.util import DevError


def manifest():
    return Registry.bundled().catalog()[0]['manifest']


def test_bundled_manifest_builds_typed_project_actions():
    spec = parse_spec(manifest())
    action = spec.actions['pipeline.get']
    assert map_arguments(action, {'pipeline_id': 7}, {'remote_project_id': 123}, {}) == {'pipeline_id': 7, 'project_id': 123}
    public = public_definition('main', 'pipeline.get', action)
    assert public['name'] == 'ext.main.pipeline.get'
    assert public['inputSchema']['additionalProperties'] is False
    assert set(public['inputSchema']['required']) == {'project', 'pipeline_id'}
    assert 'remote_project_id' not in public['inputSchema']['properties']
    assert public['annotations']['readOnlyHint'] is False


@pytest.mark.parametrize('bad', [
    {'pipeline_id': 1, 'project_id': 999}, {'pipeline_id': 1, 'account_id': 'other'},
    {'pipeline_id': 1, 'role_id': 'admin'}, {'pipeline_id': 1, 'space_id': 'other'},
    {'pipeline_id': '7'}, {'pipeline_id': True}, {'pipeline_id': 0}, {},
])
def test_invalid_or_authority_arguments_are_rejected(bad):
    with pytest.raises(DevError):
        map_arguments(parse_spec(manifest()).actions['pipeline.get'], bad, {'remote_project_id': 123}, {})


@pytest.mark.parametrize('selector', [
    {'eval': 'dangerous()'}, {'from_input': 'pipeline_id', 'literal': 999},
    {'from_project': 'undeclared'}, {'from_input': 'nested.field'}, {},
])
def test_unsafe_or_undeclared_mappings_fail_installation(selector):
    value = manifest()
    value['actions']['pipeline.get']['call']['arguments']['project_id'] = selector
    with pytest.raises(DevError, match='manifest'):
        parse_spec(value)


@pytest.mark.parametrize('change', ['version', 'open_schema', 'reserved_project', 'external_ref', 'unknown_slot', 'space_project_mapping', 'duplicate_slot', 'unknown_field'])
def test_manifest_validation_is_closed(change):
    value = manifest()
    action = value['actions']['pipeline.get']
    if change == 'version': value['api_version'] = 2
    elif change == 'open_schema': action['input_schema']['additionalProperties'] = True
    elif change == 'reserved_project': action['input_schema']['properties']['project'] = {'type': 'string'}
    elif change == 'external_ref': action['input_schema']['properties']['pipeline_id'] = {'$ref': 'https://invalid.example/schema'}
    elif change == 'unknown_slot': action['call']['tool_slot'] = 'not-declared'
    elif change == 'space_project_mapping': action['scope'] = 'space'
    elif change == 'duplicate_slot': value['backend_slots']['gitlab']['tool_slots'] *= 2
    elif change == 'unknown_field': value['python_entrypoint'] = 'untrusted:load'
    with pytest.raises(DevError): parse_spec(value)


@pytest.mark.parametrize('literal', [False, 0, None, [], {'a': [1, 2]}])
def test_literal_values_are_copied_without_truthiness_changes(literal):
    value = manifest()
    value['actions']['pipeline.list']['call']['arguments']['per_page'] = {'literal': literal}
    action = parse_spec(value).actions['pipeline.list']
    result = map_arguments(action, {}, {'remote_project_id': 123}, {})
    assert result == {'project_id': 123, 'per_page': literal}
    if isinstance(literal, dict):
        result['per_page']['a'].append(3)
        assert action.call.arguments['per_page']['literal'] == {'a': [1, 2]}


def test_registry_rejects_duplicate_ids_and_does_not_expose_mutable_definitions():
    original = manifest()
    with pytest.raises(DevError): Registry([original, copy.deepcopy(original)])
    registry = Registry([original])
    registry.get('gitlab').actions.clear()
    assert len(registry.get('gitlab').actions) == 2
    with pytest.raises(DevError): registry.get('not-installed')


def test_non_json_or_oversized_manifests_fail():
    for value in [None, [], {'api_version': 1, 'x': float('nan')}, {'api_version': 1, 'x': 'a' * 300000}]:
        with pytest.raises(DevError): parse_spec(value)


def test_rule_intersection_preserves_action_project_pairs():
    identifier = 'ext_' + 'a' * 32
    ceiling = RolePolicy(plugin_rules=[{'integration_id': identifier, 'projects': ['a'], 'actions': ['pipeline.get']}])
    current = RolePolicy(plugin_rules=[{'integration_id': identifier, 'projects': ['a', 'b'], 'actions': ['pipeline.get', 'pipeline.list']}])
    effective = RolePolicy(plugin_rules=[r.model_dump() for r in intersect(ceiling, current)])
    assert allows(effective, identifier, 'pipeline.get', 'a')
    assert not allows(effective, identifier, 'pipeline.get', 'b')
    assert not allows(effective, identifier, 'pipeline.list', 'a')
    assert not allows(effective, identifier, 'pipeline.get', '')


def test_rules_reject_implicit_space_scope_and_unbounded_surface():
    identifier = 'ext_' + 'a' * 32
    with pytest.raises(ValidationError): PluginRule(integration_id=identifier, actions=['pipeline.get'])
    with pytest.raises(ValidationError): PluginRule(integration_id=identifier, scope='space', projects=['a'], actions=['pipeline.get'])
    with pytest.raises(ValidationError):
        RolePolicy(plugin_rules=[{'integration_id': identifier, 'projects': [str(i) for i in range(100)], 'actions': ['a', 'b', 'c']}])
    assert json.loads(RolePolicy().model_dump_json())['plugin_rules'] == []
