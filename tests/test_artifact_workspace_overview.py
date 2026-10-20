"""Artifact overviews enumerate live workspace bindings without ambient selection.

Only synthetic retained artifact metadata is created here. Downloads and real
source-byte registration are covered by the served navigation/integration tests.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from tests.identity_fixtures import (team, must, set_member, project_policy,
    make_profile, make_connection, call, data)  # noqa: F401


def artifact(app, *, space='team', owner='owner', grant=None, created=None):
    source=app.state.store.one('SELECT f.* FROM space_sources s JOIN folders f ON f.id=s.folder_resource_id WHERE s.id=?',('folder-'+space,))
    identifier=uuid.uuid4().hex
    now=time.time() if created is None else created
    app.state.store.execute('''INSERT INTO artifacts
        (id,project_id,device_id,grant_id,actor,root,name,bytes,sha256,created,expires,
         source_operation_id,space_id,owner_user_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'',?,?)''',
        (identifier,'project-'+space,source['device_id'],grant,'fixture:'+owner,
         source['device_path'],'snapshot-'+identifier,3,'a'*64,now,now+3600,space,owner))
    return identifier


def overview(browser, **arguments):
    return browser.post('/api/tools/call',json={'tool':'artifacts_list','arguments':arguments})


def test_panel_overview_uses_each_visible_workspace_without_mutating_login(team):
    app,b=team
    left=artifact(app,space='team');right=artifact(app,space='other-team')
    before=app.state.store.all('SELECT * FROM sessions ORDER BY id_hash')
    all_rows=must(overview(b['owner']))['artifacts']
    assert {row['artifact_id'] for row in all_rows}=={left,right}
    scoped=must(overview(b['owner'],project='project-team'))['artifacts']
    assert [row['artifact_id'] for row in scoped]==[left]
    assert app.state.store.all('SELECT * FROM sessions ORDER BY id_hash')==before
    assert not {'space_id','active_space_id','current_space_id'}&must(b['owner'].get('/api/iam/me')).keys()


def test_member_overview_filters_other_people_other_workspaces_and_revocation(team):
    app,b=team;set_member(b['owner'],'alice',project_policy('read'))
    own=artifact(app,owner='alice')
    private=artifact(app,owner='bob');outside=artifact(app,space='other-team',owner='alice')
    result=must(overview(b['alice']))
    assert [row['artifact_id'] for row in result['artifacts']]==[own]
    assert private not in json.dumps(result) and outside not in json.dumps(result)
    set_member(b['owner'],'alice',{})
    assert must(overview(b['alice']))['artifacts']==[]
    assert b['alice'].get('/api/session').json()['authenticated']


def test_overview_cursor_paginates_the_authorized_union_without_duplicates(team):
    app,b=team
    expected=[artifact(app,space=space,created=time.time()+n)
              for n,space in enumerate(['team','other-team','team'])][::-1]
    found=[];cursor=''
    while True:
        result=must(overview(b['owner'],limit=1,cursor=cursor))
        found.extend(row['artifact_id'] for row in result['artifacts'])
        cursor=result['next_cursor']
        if not cursor:break
        assert len(found)<=len(expected)
    assert found==expected


def test_same_profile_connections_never_share_artifact_receipts(team):
    app,b=team;set_member(b['owner'],'alice',project_policy('read'))
    profile=make_profile(b['owner'],project_policy('read'))
    first=make_connection(b['alice'],profile);second=make_connection(b['alice'],profile)
    own=artifact(app,owner='alice',grant=first['connection_id'])
    hidden=artifact(app,owner='alice',grant=second['connection_id'])
    outside=artifact(app,space='other-team',owner='alice')
    result=data(call(b['alice'],first['token'],'read',{'operation':'artifacts','project':'project-team'}))
    assert [row['artifact_id'] for row in result['artifacts']]==[own]
    assert hidden not in json.dumps(result) and outside not in json.dumps(result)
    result=data(call(b['alice'],second['token'],'read',{'operation':'artifacts','project':'project-team'}))
    assert [row['artifact_id'] for row in result['artifacts']]==[hidden]


@pytest.mark.parametrize('change',['folder','membership'])
def test_overview_rechecks_mapping_and_membership_before_disclosure(team,change):
    app,b=team;set_member(b['owner'],'alice',project_policy('read'))
    identifier=artifact(app,owner='alice')
    assert must(overview(b['alice']))['artifacts'][0]['artifact_id']==identifier
    if change=='folder':
        app.state.store.execute("UPDATE folders SET device_path=device_path||'/changed',version=version+1 WHERE id='folder-team'")
    else:
        app.state.store.execute("UPDATE memberships SET active=0 WHERE space_id='team' AND user_id='alice'")
    assert must(overview(b['alice']))['artifacts']==[]
    assert app.state.store.one('SELECT id FROM artifacts WHERE id=?',(identifier,))


def test_panel_diagnostics_only_enumerates_authorized_folder_devices(team):
    app,b=team;set_member(b['owner'],'alice',project_policy('read'))
    def get(browser):
        return must(browser.post('/api/tools/call',json={'tool':'diagnostics_get','arguments':{}}))
    assert {row['id'] for row in get(b['owner'])['devices']}=={'device-team','device-other-team'}
    assert {row['id'] for row in get(b['alice'])['devices']}=={'device-team'}
    set_member(b['owner'],'alice',{})
    assert get(b['alice'])['devices']==[]
    assert not app.state.store.all('SELECT id FROM operations')
