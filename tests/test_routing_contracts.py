"""Offline bounds, source identity, hook precedence and strict-remote contracts."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.routed_execution import source_boundary, read_lease, plan_rch, validate_rch_config
from hub.project_routing import ProjectRouting, family_for
from shared.execution_hooks import plan_hooks
from shared.routing_contracts import RCHConfig, RoutedExecution, TestCommand as CommandRecipe, relative_source, mount_path
from shared.util import DevError


@pytest.mark.parametrize('value', ['../sibling', '/etc', 'a/../b', 'a\\b', 'c:/root', 'a\x00b', 'a\nb'])
def test_relative_sources_reject_escape(value):
    with pytest.raises(ValueError): relative_source(value)


def test_mount_mappings_are_for_one_relative_view():
    assert mount_path('/srv/nfs', 'apps/api') == '/srv/nfs/apps/api'
    assert mount_path('Z:\\shared', 'apps/api') == 'Z:\\shared\\apps\\api'
    assert relative_source('./apps//api') == 'apps/api'
    assert relative_source('.') == '.'


def test_boundary_blocks_sibling_symlink_and_mismatched_root(tmp_path):
    base = tmp_path / 'shared'; base.mkdir()
    child = base / 'project'; child.mkdir()
    project = {'root': str(child), '_source_boundary': {'mount': str(base), 'subpath': 'project', 'protocol': 1}}
    source_boundary(project)
    with pytest.raises(DevError): source_boundary({**project, 'root': str(tmp_path)})
    other = tmp_path / 'other'; other.mkdir()
    child.rmdir(); child.symlink_to(other, target_is_directory=True)
    with pytest.raises(DevError) as error: source_boundary(project)
    assert error.value.code == 'SOURCE_SYMLINK_BLOCKED'


def test_space_hook_cannot_be_overridden_or_executed_twice():
    hook = {'name': 'remote', 'actions': ['test'], 'handler': 'require_rch'}
    resolved = plan_hooks('test', {'provider': 'device'}, [hook], [hook])
    assert resolved['provider'] == 'rch' and resolved['local_fallback'] is False
    assert [h['source'] for h in resolved['hooks']] == ['space', 'project']
    with pytest.raises(DevError) as error:
        plan_hooks('test', {'provider': 'device'}, [hook], [{**hook, 'rch': {'worker': 'another'}}])
    assert error.value.code == 'EXECUTION_HOOK_CONFLICT'
    with pytest.raises(DevError) as error:
        plan_hooks('test', {'provider': 'rch'}, [{'name': 'deny', 'actions': ['test'], 'handler': 'deny'}])
    assert error.value.code == 'EXECUTION_HOOK_DENIED'


@pytest.mark.parametrize('config', [{'require_remote': False}, {'result_dirs': ['.']}, {'result_dirs': ['../secret']},
                                   {'result_dirs': ['.git']}, {'mode': 'compilation', 'required_tools': ['clang']},
                                   {'required_tools': ['x; sh']}])
def test_rch_configs_fail_closed(config):
    with pytest.raises(ValidationError): RCHConfig.model_validate(config)


def test_rch_argv_does_not_add_a_shell_or_accept_fallback(tmp_path):
    argv = ['python3', '-c', 'print("spaces;not shell")']
    command, env = plan_rch('/usr/bin/rch', {'mode': 'job', 'required_tools': ['clang'], 'result_dirs': ['results'],
        'worker': 'approved', 'profile': 'test'}, argv, tmp_path)
    assert command == ['/usr/bin/rch', 'exec', '--job', '--require-tool', 'clang', '--result-dir', 'results', '--', *argv]
    assert env == {'RCH_REQUIRE_REMOTE': '1', 'RCH_STATE_HOME': str(tmp_path), 'RCH_WORKER': 'approved', 'RCH_PROFILE': 'test'}
    assert validate_rch_config({})['enabled'] is False
    with pytest.raises(ValueError): validate_rch_config({'executable': './rch'})


@pytest.mark.parametrize('bad', [{'action': 'git', 'argv': ['python3']}, {'action': 'test', 'command': 'pytest; rm x'},
                                {'action': 'test', 'argv': ['x\x00y']}, {'action': 'shell', 'command': 'x', 'argv': ['x']}])
def test_invalid_routed_invocations_are_rejected(bad):
    with pytest.raises(ValidationError): RoutedExecution.model_validate({'project': 'p', 'idempotency_key': 'bounded-test', **bad})
    with pytest.raises(ValidationError): CommandRecipe(name='bad', argv=['python3'], cwd='../sibling')


def test_rch_leases_require_unique_strict_remote_identity(tmp_path):
    folder = tmp_path / 'job-leases'; folder.mkdir()
    assert read_lease(tmp_path) is None
    lease = {'schema_version': 1, 'identity': {'local_wrapper_id': 'rchw-11111111-2222-3333-4444-555555555555', 'remote_build_id': 3},
        'strict_remote': True, 'terminal_acknowledged': True}
    path = folder / 'one.json'; path.write_text(json.dumps(lease))
    assert read_lease(tmp_path)['identity'] == lease['identity']
    path.write_text(json.dumps({**lease, 'strict_remote': False}))
    assert read_lease(tmp_path) is None
    path.write_text(json.dumps(lease)); (folder / 'two.json').write_text(json.dumps(lease))
    assert read_lease(tmp_path) is None


def test_shared_source_holds_overlap_not_sibling_projects():
    row = {'id': 'prior', 'tool': 'routed_exec', 'state': 'needs_review',
        'route_context': json.dumps({'source_root_id': 'same-nfs', 'subpath': 'app'})}
    routing = ProjectRouting(SimpleNamespace(store=SimpleNamespace(all=lambda *args: [row])))
    def op(path, tool='write', source='same-nfs'):
        return {'id': 'next', 'tool': tool, 'created': 2, 'space_id': 's', 'attempts': 0,
            'route_context': json.dumps({'source_root_id': source, 'subpath': path})}
    assert routing.source_blocker(op('app/subdir')) == 'prior'
    assert routing.source_blocker(op('.')) == 'prior'
    assert routing.source_blocker(op('sibling')) is None
    assert routing.source_blocker(op('app', source='different-source')) is None
    assert routing.source_blocker(op('app', tool='routed_job_control')) is None


def test_family_resolution_is_explicit_not_command_regex():
    assert family_for('exec', {'action': 'test'}) == 'test'
    assert family_for('exec', {'command': 'pytest'}) == 'shell'
    assert family_for('exec', {'action': 'shell', 'target': 'vps:abc'}) == 'ssh'
    assert family_for('routed_job_control', {'action': 'cancel'}) == 'test'
    assert family_for('worktrees_create', {}) == 'git'
