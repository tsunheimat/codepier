"""Project dashboard reads existing scoped receipts without task tracking."""
import dataclasses
import json
import time
import uuid

import pytest

from hub.mcp_apps import attach
from shared.contracts import TOOLS, tool_definitions
from shared.integration_contracts import REMOTE_TOOLS
from shared.core_contracts import CORE_TOOLS
from shared.query_contracts import QUERY_TOOLS
from shared.util import DevError
from tests.operation_fixture import env, call, operation


def dashboard(env, **args):
    return call(env, 'workspace_status', **{'project': 'P', **args})


def result_operation(env, tool, data, **opts):
    identifier = operation(env, finish=False, **opts)
    env[0].store.execute('UPDATE operations SET tool=? WHERE id=?', (tool, identifier))
    data = {**data}
    if tool == 'validation_run': data['validation_id'] = identifier
    env[0].complete({'id': identifier}, {'ok': True, 'data': data})
    return identifier


def test_read_returns_receipts_without_progress_or_starting_operations(env):
    recent = operation(env)
    before = env[0].store.one('SELECT count(*) n FROM operations')['n']
    value = dashboard(env)
    assert not {'workflow', 'workflows', 'evidence', 'progress', 'next_step'} & value.keys()
    assert not value['execution_started']
    assert value['recent_operations'][0]['operation_id'] == recent
    assert env[0].store.one('SELECT count(*) n FROM operations')['n'] == before


def test_receipts_never_expose_logs_commands_or_environment(env):
    linked = operation(env, output='PRIVATE_LOG_MUST_BE_EXPLICITLY_OPENED')
    nearby = operation(env)
    env[0].store.execute('UPDATE operations SET args_summary=? WHERE id=?',
        (json.dumps({'command': 'PRIVATE_COMMAND', 'env': {'SECRET': 'PRIVATE_ENV'}}), linked))
    value = dashboard(env)
    assert [e['operation_id'] for e in value['recent_operations']] == [nearby, linked]
    assert 'PRIVATE_' not in json.dumps(value)


@pytest.mark.parametrize('change', ['grant', 'project', 'read_scope', 'wrong_selected_project'])
def test_dashboard_preserves_original_authorization(env, change):
    principal = env[1]
    args = {}
    if change == 'grant': principal = dataclasses.replace(principal, grant_id='other')
    if change == 'project': principal = dataclasses.replace(principal, projects=[])
    if change == 'read_scope': principal = dataclasses.replace(principal, scopes={'write'})
    if change == 'wrong_selected_project': args['project'] = 'P2'
    operation(env)
    if change in {'project', 'read_scope'}:
        with pytest.raises(DevError): dashboard((env[0], principal), **args)
    else:
        assert dashboard((env[0], principal), **args)['recent_operations'] == []


def test_worktree_filter_is_exact_and_device_changes_fence_receipts(env):
    original, worktree = operation(env), operation(env)
    wid = uuid.uuid4().hex
    env[0].store.execute('UPDATE operations SET args_summary=? WHERE id=?',
                         (json.dumps({'workspace_id': wid}), worktree))
    first = dashboard(env)
    assert [e['operation_id'] for e in first['recent_operations']] == [original]
    isolated = dashboard(env, workspace_id=wid)
    assert [e['operation_id'] for e in isolated['recent_operations']] == [worktree]
    assert isolated['root'] is None
    env[0].store.execute("INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES('d2','Second','fixture',1,'legacy','u')")
    env[0].store.execute("UPDATE projects SET device_id='d2' WHERE id='p'")
    assert dashboard(env)['recent_operations'] == []


def test_historical_validation_pass_is_never_fresh_or_accepted(env):
    passed = result_operation(env, 'validation_run', {'label': 'Test run', 'state': 'passed', 'exit_code': 0})
    failed = result_operation(env, 'validation_run', {'label': 'Failure', 'state': 'failed', 'exit_code': 7})
    values = {e['operation_id']: e for e in dashboard(env)['recent_operations']}
    assert values[passed]['validation']['historical_state'] == 'passed'
    assert values[passed]['validation']['freshness'] == 'not_checked'
    assert 'source_current' not in values[passed]['validation']
    assert values[failed]['state'] == 'failed' and values[failed]['exit_code'] == 7
    assert values[failed]['validation']['historical_state'] == 'failed'


def test_only_successful_saved_review_ref_is_projected(env):
    reference = uuid.uuid4().hex
    good = result_operation(env, 'show_changes', {'review_ref': reference, 'summary': {'files': 2},
                            'coverage': {'complete': False}, 'diff': 'PRIVATE_SOURCE_DIFF'})
    bad = result_operation(env, 'show_changes', {'review_ref': 'not-an-id', 'summary': {'files': 1}})
    values = {e['operation_id']: e for e in dashboard(env)['recent_operations']}
    assert values[good]['review']['review_ref'] == reference
    assert values[good]['review']['coverage']['complete'] is False
    assert 'review' not in values[bad] and 'PRIVATE_SOURCE_DIFF' not in json.dumps(values)


def test_private_computer_evidence_cannot_be_a_read_scope_shortcut(env):
    result_operation(env, 'computer_observe', {'images': ['PRIVATE_MEDIA']})
    for principal in (env[1], dataclasses.replace(env[1], scopes=env[1].scopes | {'computer'})):
        value = dashboard((env[0], principal))
        assert not value['recent_operations']
        assert 'PRIVATE_MEDIA' not in json.dumps(value)


def test_recent_window_is_bounded_and_explicit(env):
    identifiers = [operation(env) for _ in range(6)]
    value = dashboard(env, limit=2)
    assert [r['operation_id'] for r in value['recent_operations']] == identifiers[-1:-3:-1]
    assert value['recent_window_limited']
    assert len(dashboard(env)['recent_operations']) == 6


def test_artifact_metadata_rechecks_mapping_and_never_exposes_credentials(env):
    identifier = result_operation(env, 'tasks_run', {'exit_code': 0})
    runtime = env[0]
    runtime.store.execute('UPDATE operations SET tool=?,result=? WHERE id=?',
        ('artifacts_register', json.dumps({'ok': True, 'data': {'artifact_id': identifier}}), identifier))
    now = time.time()
    root = runtime.project('P', env[1])['root']
    runtime.store.execute('INSERT INTO artifacts(id,project_id,device_id,grant_id,actor,root,name,bytes,sha256,created,expires,source_operation_id,space_id,owner_user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (identifier, 'p', 'd', 'g', env[1].actor, root, 'source.zip', 42, 'a' * 64, now, now + 60, '', env[1].space_id, env[1].user_id))
    artifact = dashboard(env)['recent_operations'][0]['artifact']
    assert artifact['download_path'] == '/api/artifacts/' + identifier + '/download?space_id=legacy'
    assert artifact['immutable'] and not artifact['expired']
    runtime.store.execute('UPDATE artifacts SET expires=? WHERE id=?', (now - 1, identifier))
    assert dashboard(env)['recent_operations'][0]['artifact']['expired']
    runtime.store.execute("UPDATE artifacts SET root='/other-root' WHERE id=?", (identifier,))
    assert dashboard(env)['recent_operations'][0]['artifact']['unavailable']


def test_distribution_allows_only_reviewed_dashboard_evidence():
    from pathlib import Path
    from scripts.build_source_bundle import include
    prefix = Path('docs/evidence/workspace-dashboard-20260917')
    assert include(prefix/'ACCEPTANCE.md')
    assert include(prefix/'verification-summary.json')
    assert include(prefix/'screenshots/dashboard-390-dark.png')
    for path in (prefix/'raw.log', prefix/'fixture-credentials.json', prefix/'screenshots/private.png',
                 Path('web/fonts/private.woff2'), Path('web/fonts/private.ttf'), Path('.env')):
        assert not include(path)
    for path in ('hub/workspace_status.py', 'web/mcp-apps/ui.js'):
        assert include(Path(path))


def test_dashboard_query_remains_available_without_widget(env):
    assert TOOLS['workspace_status'].local and TOOLS['workspace_status'].scope == 'read'
    assert 'workspace_status' not in REMOTE_TOOLS
    for profile in ('full', 'coding'):
        tools = {tool['name']: tool for tool in tool_definitions(profile)}
        assert 'workspace_status' not in tools
        assert tools['workspace']['_meta']['ui']['visibility'] == ['model', 'app']
    assert set(t['name'] for t in tool_definitions('coding')) == CORE_TOOLS | QUERY_TOOLS | {'get_profile','get_access_context'}
    value = dashboard(env)
    bound = attach({'structuredContent': value}, 'workspace_status', {'project': 'P'}, value, lambda: 'https://panel.example')
    assert bound == {'structuredContent': value}
    assert tools['workspace']['securitySchemes'][0]['scopes'] == ['read']
