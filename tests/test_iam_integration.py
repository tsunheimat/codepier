"""Direct-member workspace identity acceptance; no Role/default-space semantics."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

import pytest

from tests.identity_fixtures import (team, Browser, must, call, data, set_member,
    make_profile, make_connection, update_profile, project_policy)  # noqa: F401


def test_account_session_has_no_ambient_workspace_or_instance_escalation(team):
    app, b = team
    me = must(b['alice'].get('/api/iam/me'))
    assert not me['instance_admin']
    assert 'team' in {s['id'] for s in me['spaces']} and 'other-team' not in {s['id'] for s in me['spaces']}
    assert not {'space_id','current_space_id','active_space_id'} & me.keys()
    assert b['alice'].get('/api/iam/users').status_code == 403
    assert b['alice'].get('/api/spaces/other-team').status_code == 403
    assert 'project-other-team' not in b['alice'].get('/api/projects').text
    assert 'space_id' not in {r['name'] for r in app.state.store.all('PRAGMA table_info(sessions)')}


@pytest.mark.parametrize('live', [False, True])
def test_connection_identity_survives_profile_edits_without_widening_fixed_consent(team, live):
    _, b = team
    full = project_policy('read','write','execute')
    set_member(b['owner'], 'alice', full)
    p = make_profile(b['owner'], project_policy('read'))
    c = make_connection(b['alice'], p, live=live)
    before = data(call(b['alice'], c['token']))
    assert before['connection']['id'] == c['connection_id'] and before['profile']['id'] == p['id']
    update_profile(b['owner'], p, full)
    after = data(call(b['alice'], c['token']))
    assert after['connection'] == before['connection'] and after['profile']['id'] == p['id']
    assert ('execute' in after['scopes']) is live
    assert {v['id'] for v in after['projects']} == {'project-team'}
    # Neither client hints nor an unrelated panel cookie can change the binding.
    hinted = data(call(b['bob'], c['token'], endpoint='/mcp?space_id=other-team&profile_id=arbitrary'))
    assert hinted == after and 'space_id' not in hinted


def test_shared_profiles_do_not_share_connections_and_resource_rights_intersect(team):
    app, b = team
    set_member(b['owner'], 'alice', project_policy('read','write','execute'))
    set_member(b['owner'], 'bob', project_policy('read'))
    p = make_profile(b['owner'], project_policy('read','write','execute'))
    a, other = make_connection(b['alice'], p), make_connection(b['bob'], p)
    assert a['connection_id'] != other['connection_id'] != p['id']
    assert 'execute' in data(call(b['alice'], a['token']))['scopes']
    assert 'execute' not in data(call(b['bob'], other['token']))['scopes']
    for user, own, hidden in [('alice',a,other),('bob',other,a)]:
        listing = must(b[user].get('/api/spaces/team/connections'))['connections']
        assert own['connection_id'] in {c['id'] for c in listing}
        assert hidden['connection_id'] not in {c['id'] for c in listing}
        denied = b[user].post('/api/spaces/team/connections/'+hidden['connection_id']+'/revoke')
        assert denied.status_code == 403 and denied.json()['error']['code'] == 'WORKSPACE_ADMIN_REQUIRED'
        assert app.state.store.one('SELECT revoked FROM grants WHERE id=?',(hidden['connection_id'],))['revoked'] == 0
    assert data(call(b['bob'], a['token']))['connection']['id'] == a['connection_id']


def test_membership_changes_apply_to_existing_connections_and_never_restore_revoked_tokens(team):
    app,b = team
    full = project_policy('read','write','execute')
    set_member(b['owner'],'alice',full)
    p = make_profile(b['owner'],full)
    c = make_connection(b['alice'],p,live=True)
    set_member(b['owner'],'alice',project_policy('read'))
    assert 'execute' not in data(call(b['alice'],c['token']))['scopes']
    must(b['owner'].put('/api/spaces/team/members/alice/suspension',json={'blocked':True}))
    assert call(b['alice'],c['token']).status_code == 403
    must(b['owner'].put('/api/spaces/team/members/alice/suspension',json={'blocked':False}))
    assert data(call(b['alice'],c['token']))['connection']['id'] == c['connection_id']
    must(b['alice'].post('/api/spaces/team/connections/'+c['connection_id']+'/revoke'))
    set_member(b['owner'],'alice',full)
    assert call(b['alice'],c['token']).status_code == 401
    assert app.state.store.one('SELECT revoked FROM grants WHERE id=?',(c['connection_id'],))['revoked'] == 1


def test_profile_is_shared_in_workspace_but_not_global_and_cannot_be_rebound(team):
    app,b = team
    p = make_profile(b['owner'],project_policy('read'))
    assert p['id'] in {r['id'] for r in must(b['bob'].get('/api/spaces/team/profiles'))['profiles']}
    assert b['owner'].get('/api/spaces/other-team/profiles/'+p['id']).status_code == 404
    with pytest.raises(sqlite3.IntegrityError):
        app.state.store.execute("UPDATE access_profiles SET space_id='other-team' WHERE id=?",(p['id'],))
    c = make_connection(b['owner'],p)
    with pytest.raises(sqlite3.IntegrityError):
        app.state.store.execute("UPDATE grants SET space_id='other-team' WHERE id=?",(c['connection_id'],))
    assert app.state.store.all('PRAGMA foreign_key_check') == []


def test_source_and_alias_resolution_cannot_escape_connection_workspace(team):
    _,b = team
    set_member(b['owner'],'alice',project_policy('read','execute'))
    p = make_profile(b['owner'],project_policy('read','execute'))
    c = make_connection(b['alice'],p)
    listing = data(call(b['alice'],c['token'],'project_query',{'operation':'list'}))
    assert 'project-team' in json.dumps(listing) and 'project-other-team' not in json.dumps(listing)
    reply = call(b['alice'],c['token'],'project_query',{'operation':'status','project':'project-other-team'})
    assert reply.json()['result']['isError'] and 'project-other-team' not in json.dumps(reply.json()['result'].get('structuredContent',{}).get('project',{}))


def test_profile_and_member_managers_cannot_delegate_beyond_direct_rights(team):
    _,b=team
    limited = project_policy('read',manage=('members','profiles'))
    set_member(b['owner'],'alice',limited)
    for policy in [project_policy('read','execute'), {'project_rules':[{'all_projects':True,'actions':['read']}]},
                   project_policy('read',manage=('workspace',))]:
        profile = b['alice'].post('/api/spaces/team/profiles',json={
            'label':'Cannot escalate','policy':policy,'idempotency_key':uuid.uuid4().hex})
        assert profile.status_code in {400,403}, profile.text
        member = b['alice'].put('/api/spaces/team/members/bob',json={
            'policy':policy,'expected_version':1,'idempotency_key':uuid.uuid4().hex})
        assert member.status_code == 403, member.text
    assert b['alice'].put('/api/spaces/team/members/owner/suspension',json={'blocked':True}).status_code == 403
    p = make_profile(b['alice'],project_policy('read'))
    assert p['policy']['project_rules'][0]['actions'] == ['read']


def test_last_workspace_manager_and_last_instance_recovery_admin_are_protected(team):
    app,b=team
    response = b['owner'].put('/api/spaces/team/members/owner',json={
        'policy':project_policy('read'),'expected_version':1,'idempotency_key':uuid.uuid4().hex})
    assert response.status_code == 409 and response.json()['error']['code'] == 'LAST_WORKSPACE_ADMIN'
    response = b['owner'].put('/api/spaces/team/members/owner/suspension',json={'blocked':True})
    assert response.status_code == 409
    row = app.state.store.one("SELECT version FROM iam_users WHERE user_id='owner'")
    response = b['owner'].put('/api/iam/users/owner',json={'active':False,'instance_admin':False,'expected_version':row['version']})
    assert response.status_code == 409 and response.json()['error']['code'] == 'LAST_ADMIN'


def test_workspace_suspend_and_restore_is_explicit_and_does_not_change_login(team):
    _,b=team
    saved=must(b['owner'].put('/api/spaces/team',json={'label':'Paused','active':False,'expected_version':1,'idempotency_key':uuid.uuid4().hex}))
    assert b['owner'].get('/api/spaces/team').status_code == 404
    assert any(s['id']=='team' for s in must(b['owner'].get('/api/iam/me'))['disabled_spaces'])
    body={'label':'Restored','active':True,'expected_version':saved['version'],'idempotency_key':uuid.uuid4().hex}
    assert b['alice'].post('/api/spaces/team/restore',json=body).status_code == 404
    must(b['owner'].post('/api/spaces/team/restore',json=body))
    assert b['owner'].get('/api/spaces/team').status_code == 200
    assert b['owner'].get('/api/spaces/other-team').status_code == 200


def test_invitation_is_bounded_single_use_and_rechecks_creator_rights(team):
    _,b=team
    invite=must(b['owner'].post('/api/spaces/team/invites',json={'policy':project_policy('read')}))
    accepted=must(b['bob'].post('/api/iam/invites/accept',json={'invitation':invite['invitation']}))
    assert accepted == {'space_id':'team'}
    assert b['alice'].post('/api/iam/invites/accept',json={'invitation':invite['invitation']}).status_code == 400
    set_member(b['owner'],'alice',project_policy('read',manage=('members',)))
    pending=must(b['alice'].post('/api/spaces/team/invites',json={'policy':project_policy('read')}))
    set_member(b['owner'],'alice',project_policy('read'))
    assert b['bob'].post('/api/iam/invites/accept',json={'invitation':pending['invitation']}).status_code == 403


def test_account_revocation_destroys_sessions_and_revokes_existing_connections(team):
    app,b=team
    set_member(b['owner'],'alice',project_policy('read'))
    p=make_profile(b['owner'],project_policy('read')); c=make_connection(b['alice'],p)
    row=app.state.store.one("SELECT version FROM iam_users WHERE user_id='alice'")
    must(b['owner'].put('/api/iam/users/alice',json={'active':False,'instance_admin':False,'expected_version':row['version']}))
    assert b['alice'].get('/api/iam/me').status_code == 401
    assert call(b['owner'],c['token']).status_code == 401
    assert not app.state.store.all("SELECT id_hash FROM sessions WHERE user_id='alice'")
    assert app.state.store.one('SELECT revoked FROM grants WHERE id=?',(c['connection_id'],))['revoked'] == 1


def test_expired_and_blocked_memberships_never_appear_in_resource_lists(team):
    app,b=team
    set_member(b['owner'],'bob',project_policy('read'))
    assert 'project-team' in b['bob'].get('/api/projects').text
    app.state.store.execute("UPDATE memberships SET expires=? WHERE user_id='bob' AND space_id='team'",(time.time()-1,))
    assert 'project-team' not in b['bob'].get('/api/projects').text
    assert 'team' not in {r['id'] for r in must(b['bob'].get('/api/spaces'))['spaces']}
