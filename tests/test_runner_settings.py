"""Owner opt-in, full-validator writes, private values and revision safety."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import validate_config
from agent.runner_settings import SettingsStore, execute, read_config, revision
from shared.instance_lock import InstanceLock
from shared.runner_settings import MASK, OWNER_FIELDS, RunnerSettingsRequest
from shared.util import DevError


@pytest.fixture
def settings(tmp_path):
    path = tmp_path/'runner.json'
    value = {'hub_url': 'http://localhost:8765', 'device_id': 'fixture', 'secret': 'private-device-secret-'*3,
             'allowed_roots': [{'path': str(tmp_path), 'writable': True, 'allow_tasks': True}],
             'shell': {'env': {'TOKEN': 'private-environment-secret'}, 'enabled': False},
             'settings_management': {'enabled': True},
             'tasks': {'one': {'command': ['echo', 'one'], 'projects': ['fixture']}}}
    path.write_text(json.dumps(value), encoding='utf-8')
    normalized = validate_config(value, path)
    return SettingsStore(path, effective=normalized, startup=normalized)


def test_snapshot_has_every_owner_group_but_never_identity_or_private_values(settings):
    value = settings.snapshot()
    assert {group['key'] for group in value['groups']} == set(OWNER_FIELDS)
    assert value['configuration']['shell']['env']['TOKEN'] == MASK
    serialized = json.dumps(value)
    assert 'private-environment-secret' not in serialized and 'private-device-secret' not in serialized
    assert not {'secret', 'device_id', 'builtin_root_policy'} & value['configuration'].keys()
    assert value['status'] == 'active' and value['editable'] is True
    assert value['readiness']['ssh']['connection_tested'] is False


def test_preview_validates_without_writing_or_claiming_applied(settings):
    old = settings.path.read_bytes()
    value = settings.apply({'shell': {'enabled': True, 'projects': ['*']}}, revision(old), preview=True)
    assert value['validated'] and not value['saved'] and not value['applied']
    assert settings.path.read_bytes() == old
    assert not (settings.path.parent/'runner-settings-backups').exists()


def test_private_placeholders_preserve_secrets_and_section_replacement_removes_entries(settings):
    old = read_config(settings.path)[1]
    snapshot = settings.snapshot()
    shell = snapshot['configuration']['shell']
    shell['enabled'] = True
    result = settings.apply({'shell': shell, 'tasks': {}}, snapshot['revision'])
    new = read_config(settings.path)[1]
    assert new['shell']['env']['TOKEN'] == 'private-environment-secret'
    assert new['secret'] == old['secret'] and new['device_id'] == old['device_id']
    assert new['allowed_roots'] == old['allowed_roots'] and new['tasks'] == {}
    assert result['saved'] and result['status'] == 'pending' and not result['applied']
    assert 'private-environment-secret' not in json.dumps(result)
    assert (settings.path.parent/'runner-settings-backups'/f"{snapshot['revision']}.json").read_text() == json.dumps(old)
    audit = (settings.path.parent/'runner-settings-audit.jsonl').read_text()
    assert 'private-environment-secret' not in audit and 'private-device-secret' not in audit


@pytest.mark.parametrize('key,value', [('secret', 'replacement'), ('device_id', 'other'), ('unexpected', 4), ('builtin_root_policy', {})])
def test_identity_unknown_and_builtin_policy_are_not_editable(settings, key, value):
    old = settings.path.read_bytes()
    with pytest.raises(DevError, match='Only documented'):
        settings.apply({key: value}, revision(old))
    assert settings.path.read_bytes() == old


@pytest.mark.parametrize('key,value', [('hub_url', 'http://other.invalid'), ('state_dir', '/other'), ('settings_management', {'enabled': False})])
def test_remote_cannot_change_its_own_opt_in_or_transport(settings, key, value):
    old = settings.path.read_bytes()
    with pytest.raises(DevError) as error:
        settings.apply({key: value}, revision(old))
    assert error.value.code == 'CONFIG_LOCAL_ONLY' and settings.path.read_bytes() == old


def test_remote_write_requires_both_live_and_saved_owner_opt_in(settings):
    settings.effective['settings_management']['enabled'] = False
    old = settings.path.read_bytes()
    assert settings.snapshot()['editable'] is False
    with pytest.raises(DevError) as error:
        settings.apply({'name': 'no'}, revision(old))
    assert error.value.code == 'CONFIG_OWNER_APPROVAL_REQUIRED'
    assert settings.path.read_bytes() == old
    value = read_config(settings.path)[1]
    value['settings_management']['enabled'] = False
    settings.path.write_text(json.dumps(value))
    settings.effective['settings_management']['enabled'] = True
    with pytest.raises(DevError) as error:
        settings.apply({'name': 'no'}, revision(settings.path.read_bytes()))
    assert error.value.code == 'CONFIG_OWNER_APPROVAL_REQUIRED'


def test_opt_in_defaults_off_and_requires_local_write(settings):
    raw, value, _ = read_config(settings.path)
    value.pop('settings_management')
    settings.path.write_text(json.dumps(value))
    config = read_config(settings.path)[2]
    assert config['settings_management']['enabled'] is False
    local = SettingsStore(settings.path)
    enabled = local.apply({'settings_management': {'enabled': True}}, revision(settings.path.read_bytes()), remote=False)
    assert enabled['saved'] and read_config(settings.path)[2]['settings_management']['enabled'] is True


def test_stale_revision_cannot_overwrite_an_owner_edit(settings):
    digest = settings.snapshot()['revision']
    settings.path.write_text(settings.path.read_text() + '\n')
    with pytest.raises(DevError) as error:
        settings.apply({'name': 'lost update'}, digest)
    assert error.value.code == 'CONFIG_REVISION_CHANGED'
    assert 'lost update' not in settings.path.read_text()


def test_validator_rejects_configuration_before_backup(settings):
    raw = settings.path.read_bytes()
    with pytest.raises(DevError) as error:
        settings.apply({'sessions': {'enabled': True, 'max_sessions': -1}}, revision(raw))
    assert error.value.code == 'CONFIG_VALIDATION_FAILED' and settings.path.read_bytes() == raw
    assert not (settings.path.parent/'runner-settings-backups').exists()


def test_rollback_is_cas_preserves_identity_and_reports_pending(settings):
    original = settings.snapshot()
    changed = settings.apply({'name': 'changed', 'tasks': {}}, original['revision'])
    restored = settings.rollback(changed['revision'], changed['backup_revision'])
    assert restored['saved'] and not restored['applied']
    assert 'name' not in read_config(settings.path)[1]
    assert read_config(settings.path)[1]['tasks']['one']['command'] == ['echo', 'one']
    with pytest.raises(DevError) as error:
        settings.rollback(changed['revision'], changed['backup_revision'])
    assert error.value.code == 'CONFIG_REVISION_CHANGED'


def test_backups_are_private_and_rollback_rejects_paths(settings):
    original = settings.snapshot()
    changed = settings.apply({'name': 'changed'}, original['revision'])
    backup = settings.path.parent/'runner-settings-backups'/f"{changed['backup_revision']}.json"
    if os.name != 'nt':
        assert backup.stat().st_mode & 0o777 == 0o600
        assert backup.parent.stat().st_mode & 0o777 == 0o700
        assert settings.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(DevError):
        settings.rollback(changed['revision'], '../../unrelated')


def test_write_failure_has_backup_and_cannot_be_reported_as_applied(settings, monkeypatch):
    raw = settings.path.read_bytes()
    def fail(*args):
        raise OSError('fixture disk failure')
    monkeypatch.setattr('agent.runner_settings.atomic_json', fail)
    with pytest.raises(DevError) as error:
        settings.apply({'name': 'not saved'}, revision(raw))
    assert error.value.code == 'CONFIG_WRITE_UNCERTAIN'
    assert settings.path.read_bytes() == raw
    assert (settings.path.parent/'runner-settings-backups'/f'{revision(raw)}.json').exists()


def test_busy_writer_does_not_overwrite(settings):
    raw = settings.path.read_bytes()
    with InstanceLock(settings.path.parent/'.runner-settings.lock'):
        with pytest.raises(DevError) as error:
            settings.apply({'name': 'blocked'}, revision(raw))
    assert error.value.code == 'CONFIG_BUSY' and settings.path.read_bytes() == raw


def test_restart_sensitive_components_never_claim_healthy(settings):
    before = settings.snapshot()
    result = settings.apply({'computer': {'call_timeout_seconds': 40}}, before['revision'])
    assert result['restart_required'] == ['computer']
    value = settings.snapshot()
    assert value['status'] == 'restart_required' and value['effective']['computer']['call_timeout_seconds'] == 45
    assert value['configuration']['computer']['call_timeout_seconds'] == 40


def test_settings_cannot_be_called_from_mcp_even_with_a_valid_config(settings):
    agent = SimpleNamespace(config_path=settings.path, config=settings.effective)
    with pytest.raises(DevError) as error:
        asyncio.run(execute(agent, 'op', RunnerSettingsRequest(idempotency_key='snapshot-fixture').model_dump(), 'mcp'))
    assert error.value.code == 'PANEL_REQUIRED'


def test_mask_without_an_original_value_is_rejected(settings):
    with pytest.raises(DevError, match='existing field'):
        settings.apply({'shell': {'env': {'NEW': MASK}}}, settings.snapshot()['revision'])


def test_secret_command_arguments_are_preserved_without_being_disclosed(settings):
    raw, value, _ = read_config(settings.path)
    value['tasks']['one']['command'] = ['echo', '--token', 'private-argv-token']
    settings.path.write_text(json.dumps(value))
    settings.effective = read_config(settings.path)[2]
    snapshot = settings.snapshot()
    assert 'private-argv-token' not in json.dumps(snapshot)
    assert snapshot['configuration']['tasks']['one']['command'] == MASK
    tasks = snapshot['configuration']['tasks']
    tasks['one']['description'] = 'updated safely'
    settings.apply({'tasks': tasks}, snapshot['revision'])
    assert read_config(settings.path)[1]['tasks']['one']['command'][-1] == 'private-argv-token'


def test_local_status_does_not_claim_to_observe_running_services(settings):
    status = SettingsStore(settings.path).snapshot()
    assert status['status'] == 'configured_unverified' and status['effective'] is None


@pytest.mark.asyncio
async def test_cancelling_a_started_config_write_never_claims_it_did_not_persist(settings, monkeypatch):
    import threading
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_apply = SettingsStore.apply
    def delayed(store, *args, **kwargs):
        started.set()
        assert release.wait(5)
        try:
            return original_apply(store, *args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(SettingsStore, 'apply', delayed)
    agent = SimpleNamespace(config_path=settings.path, config=settings.effective)
    args = RunnerSettingsRequest(operation='apply', patch={'name':'durable despite cancellation'},
        expected_revision=settings.snapshot()['revision'], idempotency_key='cancelled-settings-write').model_dump()
    task = asyncio.create_task(execute(agent, 'original-operation', args, 'panel'))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    try:
        with pytest.raises(DevError) as error:
            await task
        assert error.value.code == 'CONFIG_WRITE_UNCERTAIN'
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert read_config(settings.path)[1]['name'] == 'durable despite cancellation'


def test_masked_array_reordering_cannot_silently_move_private_values():
    from agent.runner_settings import redact, restore_private
    source = [{'name':'first','env':{'KEY':'first-private'}}, {'name':'second','env':{'KEY':'second-private'}}]
    view = redact(source)
    assert restore_private(view, source) == source
    with pytest.raises(ValueError, match='array containing private placeholders'):
        restore_private(list(reversed(view)), source)


@pytest.mark.parametrize('key', ['device_id', 'secret'])
def test_changed_enrollment_identity_fences_live_snapshot_and_write(settings, key):
    value = read_config(settings.path)[1]
    value[key] = 'replacement-local-identity-'*3
    settings.path.write_text(json.dumps(value))
    original = settings.path.read_bytes()
    with pytest.raises(DevError) as read_error:
        settings.snapshot()
    assert read_error.value.code == 'CONFIG_IDENTITY_CHANGED'
    with pytest.raises(DevError) as write_error:
        settings.apply({'name':'must not write'}, revision(original))
    assert write_error.value.code == 'CONFIG_IDENTITY_CHANGED' and settings.path.read_bytes() == original


def test_removing_explicit_default_restores_default_provenance(settings):
    value = read_config(settings.path)[1]
    value['checkpoint_max_mib'] = 100
    settings.path.write_text(json.dumps(value))
    result = settings.apply({'checkpoint_max_mib':None}, revision(settings.path.read_bytes()))
    assert result['saved'] and result['changed_fields'] == ['checkpoint_max_mib']
    assert 'checkpoint_max_mib' not in read_config(settings.path)[1]


def test_minimal_existing_roots_and_tasks_expose_all_effective_editable_defaults(settings):
    value = read_config(settings.path)[1]
    value['allowed_roots'] = [{'path':str(settings.path.parent/'project')}]
    value['tasks'] = {'minimal': {'command':['echo','safe']}}
    settings.path.write_text(json.dumps(value))
    settings.effective = read_config(settings.path)[2]
    before = settings.path.read_bytes()
    snapshot = settings.snapshot()
    assert snapshot['configuration']['allowed_roots'][0] == {
        'path':str(settings.path.parent/'project'), 'writable':True, 'allow_tasks':False}
    assert snapshot['configuration']['tasks']['minimal'] == {
        'command':['echo','safe'], 'description':'', 'projects':[], 'cwd':'.',
        'timeout':300, 'env':{}, 'allow_read_concurrency':False}
    assert snapshot['effective']['tasks'] == snapshot['configuration']['tasks']
    assert settings.path.read_bytes() == before


def test_new_root_template_requires_an_explicit_path_and_never_grants_config_directory(settings):
    snapshot = settings.snapshot()
    root = snapshot['templates']['allowed_roots']
    assert root['path'] == '' and not root['writable'] and not root['allow_tasks']
    with pytest.raises(DevError) as error:
        settings.apply({'allowed_roots':[root]}, snapshot['revision'], preview=True)
    assert error.value.code == 'CONFIG_VALIDATION_FAILED'
