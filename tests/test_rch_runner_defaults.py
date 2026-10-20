"""Disabled optional RCH never blocks a native Runner's startup configuration."""
from types import SimpleNamespace

import pytest

from agent import routed_execution
from agent.config import validate_config

pytestmark = pytest.mark.windows_core


@pytest.mark.parametrize('platform,default',[('nt',''),('posix','/usr/local/bin/rch')])
def test_disabled_defaults_use_native_path_semantics(monkeypatch,platform,default):
    monkeypatch.setattr(routed_execution,'os',SimpleNamespace(name=platform))
    assert routed_execution.validate_rch_config({})=={'enabled':False,'executable':default}
    assert routed_execution.validate_rch_config({'enabled':False,'executable':''})=={
        'enabled':False,'executable':''}


@pytest.mark.parametrize('executable',['','rch.exe','./rch.exe','/usr/local/bin/rch','C:relative.exe','x\x00y'])
def test_windows_enabled_rch_requires_explicit_absolute_executable(monkeypatch,executable):
    monkeypatch.setattr(routed_execution,'os',SimpleNamespace(name='nt'))
    with pytest.raises(ValueError):
        routed_execution.validate_rch_config({'enabled':True,'executable':executable})


def test_windows_opt_in_does_not_invent_a_program_path(monkeypatch):
    monkeypatch.setattr(routed_execution,'os',SimpleNamespace(name='nt'))
    with pytest.raises(ValueError):routed_execution.validate_rch_config({'enabled':True})
    path=r'C:\Program Files\RCH\rch.exe'
    assert routed_execution.validate_rch_config({'enabled':True,'executable':path})=={
        'enabled':True,'executable':path}


@pytest.mark.parametrize('config',[{'enabled':1},{'executable':None},{'unknown':True}])
def test_disabled_status_does_not_accept_malformed_configuration(config):
    with pytest.raises(ValueError):routed_execution.validate_rch_config(config)


def test_complete_minimal_runner_configuration_on_the_actual_platform(tmp_path):
    # Runs natively on Windows CI, not a mocked Agent or a bypass of startup.
    result=validate_config({'hub_url':'http://127.0.0.1:9','device_id':'fixture','secret':'x'*43,
        'state_dir':str(tmp_path/'state'),'allowed_roots':[],'tasks':{}},tmp_path/'config.json')
    assert result['rch']['enabled'] is False
