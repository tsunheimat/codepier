"""Workspace source ownership, direct policy, fixed consent and no ambient state."""
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from hub import iam
from hub.permissions import AccessPolicy, connection_policy, project_actions, require_access
from hub.workspace_sources import (source_views, select_view, resolve_shell, validate_project_shell,
                                    execution_options, require_target_consent)
from shared.util import DevError
from shared.workspace_contracts import ShellSettings
from tests.workspace_fixtures import WorkspaceFixture


@pytest.fixture
def ws(tmp_path):
    fixture=WorkspaceFixture(tmp_path/'hub')
    yield fixture
    fixture.close()


def test_all_source_devices_is_not_all_workspace_or_inventory_devices(ws):
    source=source_views(ws.store,ws.project)
    assert set(source['views'])=={'a','b'}  # c belongs to the workspace AND the pool, but not this binding.
    assert resolve_shell(ws.store,ws.project)['allowed_device_ids']=={'a','b'}
    assert select_view(ws.store,ws.project,'shell')['device_id']=='a'
    assert select_view(ws.store,ws.project,'files')['device_id']=='b'
    with pytest.raises(DevError,match='outside its workspace folder'):
        select_view(ws.store,ws.project,'shell','c')


def test_structured_paths_are_mapped_not_commands(ws):
    view=select_view(ws.store,ws.project,'shell','b')
    assert view['root']=='/mnt/b/team/codepier'
    assert view['logical_root']=='/storage/shared/team/codepier'
    assert view['source_key']=='pool:pool'
    assert view['source_path']=='team/codepier'


def test_project_default_can_only_choose_its_source(ws):
    validate_project_shell(ws.store,ws.project,ShellSettings(mode='custom',default_device_id='b'))
    with pytest.raises(DevError):
        validate_project_shell(ws.store,ws.project,ShellSettings(mode='custom',default_device_id='c'))


def test_workspace_restriction_cannot_be_undone_by_project(ws):
    with ws.store.transaction():
        ws.store.db.execute('INSERT INTO space_shell_settings(space_id,settings) VALUES(?,?)',('alice',json.dumps({'mode':'custom','default_device_id':'a','allowed_device_ids':['a']})))
    with pytest.raises(DevError):
        validate_project_shell(ws.store,ws.project,ShellSettings(mode='custom',default_device_id='b'))
    with ws.store.transaction():
        ws.store.db.execute('UPDATE space_shell_settings SET settings=? WHERE space_id=?',(json.dumps({'mode':'disabled'}),'alice'))
    with pytest.raises(DevError,match='disabled'):
        resolve_shell(ws.store,ws.project,project_override=ShellSettings(mode='custom',default_device_id='a'))


def test_disabled_default_never_falls_back(ws):
    ws.store.execute('UPDATE devices SET enabled=0 WHERE id=?',('a',))
    with pytest.raises(DevError,match='no other device'):
        select_view(ws.store,ws.project,'shell')


def test_direct_member_policy_keeps_action_resource_pairs(ws):
    policy={'project_rules':[{'projects':['project'],'actions':['read','execute']},{'projects':['other'],'actions':['read']}]}
    ws.store.execute("UPDATE memberships SET policy=? WHERE space_id='alice' AND user_id='alice'",(json.dumps(policy),))
    require_access(ws.store,ws.principal,'execute',project_id='project')
    with pytest.raises(DevError):
        require_access(ws.store,ws.principal,'execute',project_id='other')


def test_fixed_connection_has_its_own_identity_and_cannot_expand(ws):
    result,principal=ws.connect()
    assert principal.grant_id==result['connection_id']
    assert principal.space_id=='alice'
    assert principal.profile_id=='prf_'+'1'*32
    grant=ws.store.one('SELECT * FROM grants WHERE id=?',(principal.grant_id,))
    assert project_actions(connection_policy(ws.store,grant),'project')=={'read','write','execute'}
    ws.store.execute('UPDATE access_profiles SET policy=?,version=version+1 WHERE id=?',
        (json.dumps({'project_rules':[{'all_projects':True,'actions':['read','write','execute']}]}),principal.profile_id))
    assert not project_actions(connection_policy(ws.store,grant),'other')
    assert principal.grant_id==result['connection_id']


def test_member_revocation_narrows_both_connection_modes(ws):
    _,principal=ws.connect(consent_mode='live')
    ws.store.execute("UPDATE memberships SET policy=? WHERE space_id='alice' AND user_id='alice'",(json.dumps({'project_rules':[{'all_projects':True,'actions':['read']}]}),))
    with pytest.raises(DevError):
        require_access(ws.store,principal,'execute',project_id='project')
    require_access(ws.store,principal,'read',project_id='project')


def test_fixed_source_binding_does_not_redirect_after_mapping_change(ws):
    _,principal=ws.connect()
    require_target_consent(ws.store,principal,ws.project,select_view(ws.store,ws.project,'shell'))
    ws.store.execute("UPDATE storage_mappings SET path='/different',version=version+1 WHERE id='map_a'")
    with pytest.raises(DevError,match='fixed connection consent'):
        require_target_consent(ws.store,principal,ws.project,select_view(ws.store,ws.project,'shell'))


def test_options_do_not_expose_unbound_devices(ws):
    _,principal=ws.connect()
    result=execution_options(ws.store,SimpleNamespace(online=lambda _:False),principal,ws.project)
    assert {d['device_id'] for d in result['devices']}=={'a','b'}
    assert 'space_id' not in result
    assert result['default_device_id']=='a'


def test_connection_cannot_select_another_workspace(ws):
    _,principal=ws.connect()
    from dataclasses import replace
    with pytest.raises(DevError):
        iam.live_principal(ws.store,replace(principal,space_id='bob'))


def test_panel_account_has_no_default_workspace(ws):
    with ws.store.transaction():
        session=ws.auth.new_session('alice')
    base={'type':'http','method':'GET','path':'/api/iam/me','scheme':'https','server':('test.invalid',443),'query_string':b'',
          'headers':[(b'cookie',('rd_session='+session['cookie']).encode())]}
    principal=ws.auth.panel(Request(base))
    assert principal.space_id is None
    assert principal.projects==[]
    with pytest.raises(DevError,match='ambient workspace header'):
        ws.auth.panel(Request({**base,'headers':base['headers']+[(b'x-codepier-space',b'alice')]}))
    scoped=ws.auth.panel(Request({**base,'path_params':{'space_id':'alice'}}))
    assert scoped.space_id=='alice'


def test_profile_disable_is_policy_stop_not_identity_replacement(ws):
    _,principal=ws.connect()
    ws.store.execute('UPDATE access_profiles SET enabled=0 WHERE id=?',(principal.profile_id,))
    from hub.access_profiles import refresh_profile_principal,current_profile
    fresh=refresh_profile_principal(ws.store,principal)
    assert fresh.grant_id==principal.grant_id and fresh.projects==[]
    assert current_profile(ws.store,fresh)[0]['id']==principal.grant_id
