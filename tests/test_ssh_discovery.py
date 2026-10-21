"""SSH inventory never runs OpenSSH, Match exec, proxy commands or DNS."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.ssh import target_config, validate_ssh
from agent.ssh_discovery import approved_alias, discover, scan_files, target_identity
from shared.util import DevError


def test_default_user_and_system_configs_includes_patterns_and_negations(tmp_path):
    home = tmp_path/'home'; (home/'.ssh/hosts').mkdir(parents=True)
    system = tmp_path/'system/ssh_config'; system.parent.mkdir()
    (home/'.ssh/config').write_text('Host local first\nInclude hosts/*.conf\nHost *.internal !denied ?other\n')
    (home/'.ssh/hosts/z.conf').write_text('Host last\n')
    (home/'.ssh/hosts/a.conf').write_text('Host included\n')
    system.write_text('Host system\nInclude system-extra\n')
    (system.parent/'system-extra').write_text('Host system-included\n')
    value = scan_files(home=home, system=system)
    assert value['aliases'] == ['first','included','last','local','system','system-included']
    assert value['complete'] and 'host_patterns_not_selectable' in value['warnings']


def test_custom_F_excludes_system_and_relative_include_uses_user_ssh_directory(tmp_path):
    home = tmp_path/'home'; (home/'.ssh').mkdir(parents=True)
    system = tmp_path/'system'; system.write_text('Host must-not-appear\n')
    alternative = tmp_path/'alternative'; alternative.write_text('Host selected\nInclude relative\n')
    (home/'.ssh/relative').write_text('Host correct-relative\n')
    (tmp_path/'relative').write_text('Host wrong-relative\n')
    value = scan_files(alternative, home=home, system=system)
    assert value['aliases'] == ['correct-relative','selected']


def test_discovery_never_executes_match_or_proxy_rules(tmp_path, monkeypatch):
    config = tmp_path/'ssh_config'; marker = tmp_path/'executed'
    config.write_text(f'Host=literal\nMatch exec "touch {marker}"\n  HostName private.invalid\nProxyCommand touch {marker}\nLocalCommand touch {marker}\nKnownHostsCommand touch {marker}\n')
    def forbidden(*args, **kwargs):
        pytest.fail('Discovery attempted to execute a process')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    monkeypatch.setattr('os.system', forbidden)
    value = scan_files(config)
    assert value['aliases'] == ['literal'] and not marker.exists()
    assert 'match_rules_not_evaluated' in value['warnings']
    assert 'executable_rules_not_evaluated' in value['warnings']
    assert 'private.invalid' not in json.dumps(value)


def test_cycle_and_dynamic_include_are_bounded_and_not_silently_complete(tmp_path):
    config = tmp_path/'ssh_config'; config.write_text(f'Host one\nInclude {config}\nInclude %h.conf\n')
    value = scan_files(config)
    assert value['aliases'] == ['one'] and not value['complete']
    assert {'include_cycle','dynamic_include'} <= set(value['warnings'])


def test_oversized_and_non_regular_config_do_not_block(tmp_path):
    config = tmp_path/'ssh_config'; config.write_bytes(b'X'*(1024*1024+1))
    value = scan_files(config)
    assert not value['complete'] and value['aliases'] == []
    assert not scan_files(tmp_path)['complete']


def test_target_identity_covers_included_file_changes_and_never_leaks_contents(tmp_path):
    config = tmp_path/'ssh_config'; child = tmp_path/'included'
    config.write_text(f'Include {child}\n'); child.write_text('Host fixture\n HostName original.invalid\n')
    target = validate_ssh({'targets': {'fixture': {'host_alias': 'fixture','config_file': str(config)}}})['targets']['fixture']
    first = target_identity(target)
    child.write_text('Host fixture\n HostName changed.invalid\n')
    assert target_identity(target) != first
    assert len(first) == 64


def test_discovered_alias_is_not_owner_authorization(tmp_path):
    config = tmp_path/'ssh_config'; config.write_text('Host found\n')
    settings = {'ssh': validate_ssh({'enabled': True, 'config_files': [str(config)]})}
    value = discover(settings)
    host = next(host for host in value['hosts'] if host['host_alias'] == 'found')
    assert host['discovered'] and not host['owner_approved'] and host['status'] == 'discovered'
    with pytest.raises(DevError) as error:
        approved_alias(settings, 'found')
    assert error.value.code == 'SSH_ALIAS_NOT_APPROVED'


def test_duplicate_approved_alias_is_ambiguous_not_first_match(tmp_path):
    settings = {'ssh': validate_ssh({'enabled': True, 'targets': {'a': {'host_alias':'same'},'b':{'host_alias':'same'}}})}
    with pytest.raises(DevError) as error:
        approved_alias(settings, 'same')
    assert error.value.code == 'SSH_ALIAS_AMBIGUOUS'


def test_resource_owned_transport_is_fenced_against_local_config_changes(tmp_path):
    config = tmp_path/'ssh_config'; config.write_text('Host fixture\n HostName first.invalid\n')
    settings = {'ssh': validate_ssh({'enabled': True, 'targets': {'local': {'host_alias':'fixture','config_file':str(config)}}})}
    identity = approved_alias(settings, 'fixture')
    context = {'id':'binding','resource_id':'resource',**identity}
    project = {'_vps_context':context,'_ssh_target':{'protocol':2,'vps_id':'binding','resource_id':'resource',**identity}}
    assert target_config(settings, project)['host_alias'] == 'fixture'
    config.write_text('Host fixture\n HostName different.invalid\n')
    with pytest.raises(DevError) as error:
        target_config(settings, project)
    assert error.value.code == 'SSH_TARGET_CHANGED'


def test_binding_owned_legacy_transport_is_rejected_without_fallback():
    settings = {'ssh':validate_ssh({'enabled':True,'targets':{'local':{'host_alias':'fixture'}}})}
    project = {'_vps_context':{'id':'binding','resource_id':'resource','connection_ref':'local'},
               '_ssh_target':{'vps_id':'binding','resource_id':'resource','connection_ref':'local'}}
    with pytest.raises(DevError):
        target_config(settings, project)


def test_explicit_missing_config_is_unavailable_not_a_configured_alias(tmp_path):
    settings = {'ssh':validate_ssh({'enabled':True, 'targets':{'one':{
        'host_alias':'missing-fixture', 'config_file':str(tmp_path/'missing')}}})}
    result = discover(settings)
    host = next(item for item in result['hosts'] if item['host_alias']=='missing-fixture')
    assert host['status']=='unavailable' and not host['owner_approved']
    assert host['error']=='SSH_CONFIG_UNAVAILABLE'
    assert str(tmp_path) not in json.dumps(result)
