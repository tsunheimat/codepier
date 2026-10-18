"""Runner-owned OpenSSH regression contracts; no live host or password adapter.

Replaces password/sshpass tests with saved-reference authorization. Preserve
injection, secret-output, process receipt, failure and cancellation coverage under
the new transport. Real workspace admission/revocation cases live in test_vps.
"""
from __future__ import annotations

import copy
import json
import shlex
import subprocess
import uuid

import pytest
from pydantic import ValidationError

from agent.execution import SSHBackend
from agent.ssh import remote_script, ssh_argv, ssh_error, target_config, target_fingerprint, validate_ssh
from shared.contracts import _compact_input_schema
from shared.core_contracts import Exec
from shared.secret_output import SecretOutput
from shared.util import DevError
from tests.test_execution_workspace import setup
from tests.workspace_stack import workspace_stack

SECRET = "fixture-only-'$; with spaces"
TARGET = 'vps:' + 'a'*32


def saved_project():
    return {'id': None, 'root': '', '_vps_context': {
        'id': 'a'*32, 'resource_id': 'server', 'connection_ref': 'prod'},
        '_ssh_target': {'vps_id': 'a'*32, 'resource_id': 'server', 'connection_ref': 'prod'}}


@pytest.mark.parametrize('alias', ['-oProxyCommand=evil', 'a;id', 'user@host',
    'https://host', 'host:22', 'a b', '*', 'x\nHost other', 'x\x00', ''])
def test_host_alias_is_one_saved_literal_not_arguments(alias):
    with pytest.raises(ValueError):
        validate_ssh({'enabled': True, 'targets': {'prod': {'host_alias': alias}}})


@pytest.mark.parametrize('field,value', [('password', SECRET), ('username', 'root'),
    ('port', 22), ('host', 'elsewhere.invalid'), ('config_file', '/tmp/agent-selected')])
def test_mcp_cannot_supply_credentials_or_transport_settings(field, value):
    with pytest.raises(ValidationError):
        Exec(target=TARGET, command='true', idempotency_key=uuid.uuid4().hex, **{field: value})


@pytest.mark.parametrize('settings', [None, [], {'enabled': 1}, {'max_timeout_seconds': True},
    {'max_timeout_seconds': 0}, {'max_timeout_seconds': 86401}, {'password': SECRET},
    {'targets': {'-bad ref': {'host_alias': 'prod'}}},
    {'targets': {'prod': {'host_alias': 'prod', 'password': SECRET}}},
    {'targets': {'prod': {'host_alias': 'prod', 'host_key_policy': 'no'}}},
    {'targets': {'prod': {'host_alias': 'prod', 'shell': 'sh;id'}}},
    {'targets': {'prod': {'host_alias': 'prod', 'config_file': './config'}}}])
def test_runner_configuration_rejects_unknown_or_unsafe_inputs(settings):
    with pytest.raises(ValueError):
        validate_ssh(settings)


@pytest.mark.parametrize('denial,code', [('disabled', 'SSH_DISABLED'), ('builtin', 'SSH_DISABLED'),
    ('missing_binding', 'VPS_TRANSPORT_MISSING'), ('wrong_vps', 'VPS_TRANSPORT_MISSING'),
    ('wrong_resource', 'VPS_TRANSPORT_MISSING'), ('wrong_reference', 'VPS_TRANSPORT_MISSING'),
    ('unlisted', 'SSH_TARGET_NOT_CONFIGURED')])
def test_authority_checked_before_even_resolving_openssh(tmp_path, monkeypatch, denial, code):
    agent, _ = setup(tmp_path)
    project = saved_project()
    if denial == 'disabled': agent.config['ssh']['enabled'] = False
    elif denial == 'builtin': agent.config['builtin_root_policy'] = True
    elif denial == 'missing_binding': project.pop('_vps_context')
    elif denial == 'wrong_vps': project['_ssh_target']['vps_id'] = 'b'*32
    elif denial == 'wrong_resource': project['_ssh_target']['resource_id'] = 'elsewhere'
    elif denial == 'wrong_reference': project['_ssh_target']['connection_ref'] = 'other'
    else: agent.config['ssh']['targets'].clear()
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **kw: pytest.fail('Unauthorized target reached transport lookup'))
    with pytest.raises(DevError) as failure:
        ssh_argv(agent.config, project)
    assert failure.value.code == code


@pytest.mark.parametrize('tty,policy', [(False, 'strict'), (True, 'strict'), (False, 'accept-new')])
def test_ssh_argv_preserves_config_file_as_a_single_argument(tmp_path, monkeypatch, tty, policy):
    agent, _ = setup(tmp_path)
    config = tmp_path / 'trusted config with spaces'
    config.write_text('Host production\n  HostName fixture.invalid\n')
    agent.config['ssh']['targets']['prod'].update(config_file=str(config), host_key_policy=policy)
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **kw: '/usr/bin/ssh')
    argv = ssh_argv(agent.config, saved_project(), tty=tty)
    assert argv[0:2] == ['/usr/bin/ssh', '-tt' if tty else '-T']
    assert argv[-2:] == ['--', 'production']
    assert argv[argv.index('-F')+1] == str(config)
    assert 'BatchMode=yes' in argv
    assert 'StrictHostKeyChecking=' + ('yes' if policy == 'strict' else 'accept-new') in argv
    assert not any('sshpass' in value or SECRET in value for value in argv)


def test_dependency_config_and_command_timeout_fail_closed(tmp_path, monkeypatch):
    agent, _ = setup(tmp_path)
    project = saved_project()
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **kw: None)
    with pytest.raises(DevError) as missing:
        ssh_argv(agent.config, project)
    assert missing.value.code == 'SSH_DEPENDENCY_MISSING'
    monkeypatch.setattr('agent.ssh.shutil.which', lambda *a, **kw: '/usr/bin/ssh')
    agent.config['ssh']['targets']['prod']['config_file'] = str(tmp_path/'absent')
    with pytest.raises(DevError) as missing_config:
        ssh_argv(agent.config, project)
    assert missing_config.value.code == 'SSH_CONFIG_UNAVAILABLE'
    agent.config['ssh']['targets']['prod']['config_file'] = None
    agent.config['ssh']['max_timeout_seconds'] = 5
    with pytest.raises(DevError) as timeout:
        SSHBackend(agent).plan(project, {'command': 'true', 'timeout_seconds': 6})
    assert timeout.value.code == 'SSH_TIMEOUT_LIMIT'


def test_saved_target_does_not_require_a_local_project_folder(tmp_path):
    agent, _ = setup(tmp_path)
    agent.config['shell']['enabled'] = False
    assert target_config(agent.config, saved_project())['host_alias'] == 'production'


def test_fingerprint_changes_with_connection_and_config_bytes(tmp_path):
    agent, _ = setup(tmp_path)
    config = tmp_path/'config'
    config.write_text('Host production\n  HostName first.invalid\n')
    agent.config['ssh']['targets']['prod']['config_file'] = str(config)
    project = saved_project()
    first = target_fingerprint(agent.config, project)
    assert first == target_fingerprint(copy.deepcopy(agent.config), project)
    config.write_text('Host production\n  HostName second.invalid\n')
    second = target_fingerprint(agent.config, project)
    assert second != first
    agent.config['ssh']['targets']['prod']['host_alias'] = 'another'
    assert target_fingerprint(agent.config, project) not in {first, second}


def test_remote_cwd_and_environment_are_quoted_as_data(tmp_path):
    cwd = tmp_path / "folder '$(touch SHOULD_NOT_EXIST)"
    cwd.mkdir()
    value = "x'; touch SHOULD_NOT_EXIST; echo '"
    script = remote_script({'cwd': str(cwd), 'env': {'VALUE': value}}, 'printf "%s" "$VALUE"')
    result = subprocess.run(['/bin/sh', '-c', script], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert result.stdout == value
    assert not (tmp_path/'SHOULD_NOT_EXIST').exists()
    assert not (cwd/'SHOULD_NOT_EXIST').exists()


@pytest.mark.parametrize('env', [{'X;touch BAD': 'x'}, {'1BAD': 'x'}, {'X': 'a\x00b'}])
def test_invalid_remote_env_rejected(env):
    with pytest.raises(DevError) as failure:
        remote_script({'env': env}, 'true')
    assert failure.value.code == 'INVALID_ENV'


@pytest.mark.parametrize('secret', ['abcabc', 'x', SECRET, '中文🔑密码'])
def test_stream_redaction_all_split_positions(secret):
    value = 'before:' + secret + ':between:' + secret + ':after'
    for split in range(len(value)+1):
        redactor = SecretOutput(secret)
        output = redactor.feed(value[:split]) + redactor.feed(value[split:]) + redactor.feed('', final=True)
        assert output == value.replace(secret, '<redacted>')


@pytest.mark.parametrize('result,code', [({'exit_code': 0}, None),
    ({'cancelled': True}, 'SSH_CANCELLED'), ({'timed_out': True}, 'SSH_TIMEOUT'),
    ({'exit_code': 255, 'output': 'Permission denied (publickey).'}, 'SSH_AUTH_FAILED'),
    ({'exit_code': 255, 'output': 'Host key verification failed.'}, 'SSH_HOST_KEY_UNTRUSTED'),
    ({'exit_code': 255, 'output': 'REMOTE HOST IDENTIFICATION HAS CHANGED!'}, 'SSH_HOST_KEY_CHANGED'),
    ({'exit_code': 7, 'output': 'remote exit'}, 'SSH_COMMAND_OR_CONNECTION_FAILED')])
def test_ssh_error_classification(result, code):
    assert ssh_error(result) == code


def test_compact_schema_preserves_named_title_fields_and_validation():
    schema = {'title': 'Display', 'type': 'object', 'properties': {
        'title': {'title': 'User title', 'type': 'string', 'minLength': 1}}, 'required': ['title']}
    compact = _compact_input_schema(schema)
    assert compact['properties']['title'] == {'type': 'string', 'minLength': 1}
    assert compact['required'] == ['title'] and schema['title'] == 'Display'


@pytest.mark.integration
def test_mcp_saved_ssh_real_process_receipt_once_and_no_credential_fields(tmp_path):
    with workspace_stack(tmp_path/'ssh') as server:
        count = server.directory/'count'
        arguments = {'target': TARGET, 'command': 'printf once >> ' + shlex.quote(str(count)) + '; printf remote-ok',
                     'idempotency_key': uuid.uuid4().hex, 'yield_seconds': 0}
        first = server.mcp('exec', arguments)
        second = server.mcp('exec', arguments)
        assert first['operation_id'] == second['operation_id']
        result = server.finish(first)
        assert result['output'] == 'remote-ok'
        assert count.read_text() == 'once'
        public = server.mcp('vps')['vps'][0]
        assert not {'password', 'private_key', 'config_file', 'connection_ref'} & public.keys()
        assert SECRET not in json.dumps(public)


@pytest.mark.integration
def test_hub_restart_recovers_original_ssh_operation_without_reexecution(tmp_path):
    from tests.support import wait_for
    with workspace_stack(tmp_path/'recovery') as server:
        count = server.directory/'count'
        values = {'target': TARGET, 'command': 'printf once >> '+shlex.quote(str(count))+
            '; sleep 5; printf recovered-ssh-output', 'yield_seconds': 0,
            'idempotency_key': uuid.uuid4().hex}
        receipt = server.mcp('exec', values)
        identity = server.mcp('get_access_context')['connection']['id']
        wait_for(count.exists, timeout=15)
        server.restart_hub()
        assert server.mcp('get_access_context')['connection']['id'] == identity
        assert server.mcp('exec', values)['operation_id'] == receipt['operation_id']
        result = server.finish(receipt)
        assert result['exit_code'] == 0 and result['output'] == 'recovered-ssh-output'
        assert count.read_text() == 'once'


@pytest.mark.integration
def test_local_shell_secret_output_filter_survives_transport_refactor(tmp_path):
    # Output filtering remains useful independently of the removed SSH password
    # transport. Use only a synthetic environment value in the test Runner.
    with workspace_stack(tmp_path/'redaction') as server:
        receipt = server.mcp('exec', {'project': 'codepier', 'command': 'printf "%s" "$SSHPASS"',
            'env': {'SSHPASS': SECRET}, 'yield_seconds': 0, 'idempotency_key': uuid.uuid4().hex})
        result = server.finish(receipt)
        assert result['output'] == '<redacted>'
        row = server.must(server.client.get('/api/operations/'+receipt['operation_id']))
        assert SECRET not in json.dumps(row)
