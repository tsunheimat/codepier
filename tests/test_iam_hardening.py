"""Cross-workspace replay and late revocation against current resource bindings."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import time
import uuid

from fastapi import Request
import pytest

from hub.native_cli import make_native_router
from hub.principal import Principal
from shared.native_cli import database
from shared.util import DevError
from shared.tool_protocol import advertisement, catalog_digest, negotiate
from tests.identity_fixtures import (team, must, call, data, set_member, make_profile,
    make_connection, project_policy)  # noqa: F401
from tests.test_oidc_integration import oidc, login  # noqa: F401


def request_for(browser, path='/api/native/sessions', *, project=None, sid=None):
    return Request({'type':'http','method':'GET','path':path,
        'query_string':('project='+project).encode() if project else b'',
        'path_params':{'sid':sid} if sid else {},
        'headers':[(b'cookie',('rd_session='+browser.cookie).encode())]})


def test_operation_key_is_scoped_to_immutable_connection_workspace(team):
    app,b=team
    left_profile=make_profile(b['owner'],project_policy('read'))
    right_profile=make_profile(b['owner'],{'project_rules':[
        {'projects':['project-other-team'],'actions':['read']}]},space='other-team')
    left=make_connection(b['owner'],left_profile)
    right=make_connection(b['owner'],right_profile,space='other-team')
    arguments={'project':'same-alias','path':'README.md','idempotency_key':'shared-tab-operation-key'}
    l=data(call(b['owner'],left['token'],'read',arguments))
    r=data(call(b['owner'],right['token'],'read',arguments))
    assert l['operation_id']!=r['operation_id']
    assert data(call(b['owner'],left['token'],'read',arguments))['operation_id']==l['operation_id']
    assert data(call(b['owner'],right['token'],'read',arguments))['operation_id']==r['operation_id']
    rows=app.state.store.all('SELECT space_id,project_id FROM operations WHERE idem=?',(arguments['idempotency_key'],))
    assert {(x['space_id'],x['project_id']) for x in rows}=={('team','project-team'),('other-team','project-other-team')}
    denied=call(b['owner'],left['token'],'process',{'operation':'get','operation_ids':[r['operation_id']]})
    assert denied.json()['result']['isError'] is True


@pytest.mark.parametrize('table,identifier',[('space_folders','folder-team'),('projects','project-team')])
def test_bound_resource_workspace_cannot_change_under_children(team, table, identifier):
    app,_=team
    with pytest.raises(sqlite3.IntegrityError,match='workspace identity is immutable'):
        app.state.store.execute(f"UPDATE {table} SET space_id='other-team' WHERE id=?",(identifier,))
    assert app.state.store.one(f'SELECT space_id FROM {table} WHERE id=?',(identifier,))['space_id']=='team'


def test_suspended_inviter_cannot_admit_members_from_old_invitation(team):
    app,b=team;store=app.state.store
    set_member(b['owner'],'alice',project_policy('read',manage=('members',)))
    invitation=must(b['alice'].post('/api/spaces/team/invites',json={'policy':project_policy('read')}),201)['invitation']
    version=store.one("SELECT version FROM iam_users WHERE user_id='alice'")['version']
    must(b['owner'].put('/api/iam/users/alice',json={'active':False,'instance_admin':False,'expected_version':version}))
    before=store.all("SELECT * FROM memberships WHERE user_id='bob'")
    response=b['bob'].post('/api/iam/invites/accept',json={'invitation':invitation})
    assert response.status_code in {400,401,403},response.text
    assert store.all("SELECT * FROM memberships WHERE user_id='bob'")==before
    assert not store.all("SELECT * FROM space_invites WHERE used_by='bob'")


def test_member_manager_cannot_suspend_workspace_administrator(team):
    app,b=team;store=app.state.store
    set_member(b['owner'],'alice',project_policy('read',manage=('members',)))
    set_member(b['owner'],'bob',project_policy('read',manage=('workspace',)))
    result=b['alice'].put('/api/spaces/team/members/owner/suspension',json={'blocked':True})
    assert result.status_code==403 and result.json()['error']['code']=='WORKSPACE_ADMIN_REQUIRED'
    assert not store.one("SELECT 1 FROM membership_blocks WHERE space_id='team' AND user_id='owner'")


def test_last_local_recovery_admin_cannot_be_demoted_with_only_oidc_admin_remaining(team):
    app,b=team;store=app.state.store
    store.execute("UPDATE iam_users SET instance_admin=1,local_login=0 WHERE user_id='alice'")
    version=store.one("SELECT version FROM iam_users WHERE user_id='owner'")['version']
    result=b['owner'].put('/api/iam/users/owner',json={'active':True,'instance_admin':False,'expected_version':version})
    assert result.status_code==409 and result.json()['error']['code']=='LAST_RECOVERY_ADMIN'
    assert store.one("SELECT instance_admin FROM iam_users WHERE user_id='owner'")['instance_admin']==1


def test_device_owner_manages_inventory_lifecycle_without_workspace_administration(team, monkeypatch):
    app,b=team;store=app.state.store;runtime=app.state.runtime
    store.execute("UPDATE devices SET owner_user_id='alice',info=? WHERE id='device-team'",(json.dumps({'device_actions':['agent_restart']}),))
    monkeypatch.setattr(runtime,'online',lambda _:True)
    calls=[]
    async def dispatch(name,args,project,principal,**kwargs):
        calls.append((name,principal.user_id,principal.admin))
        return {'pending':True,'operation_id':'fixture-operation'}
    monkeypatch.setattr(runtime,'dispatch',dispatch)
    alice=app.state.auth.panel(request_for(b['alice']))
    assert not alice.admin and alice.space_id is None
    result=asyncio.run(runtime.dispatch_device_action('agent_restart',{'idempotency_key':'owner-restart'},'device-team',alice))
    assert result['pending'] and calls==[('agent_restart','alice',False)]
    bob=app.state.auth.panel(request_for(b['bob']))
    with pytest.raises(DevError): asyncio.run(runtime.dispatch_device_action('agent_restart',{},'device-team',bob))
    assert len(calls)==1


def test_pending_receipt_revalidates_membership_after_wait(team, monkeypatch):
    app,b=team;set_member(b['owner'],'alice',project_policy('read'))
    runtime=app.state.runtime
    principal=app.state.auth.panel(request_for(b['alice'],project='project-team'))
    project=runtime.project('project-team',principal)
    runtime.wait_seconds=.01
    monkeypatch.setattr(runtime,'online',lambda _:True)
    original=runtime.store.one
    # This test controls only an unresolved transport wait, never auth or
    # admission. The fake connection explicitly speaks the current protocol.
    from types import SimpleNamespace
    runtime.connections['device-team']=SimpleNamespace(workspace_protocol=1,project_routing_protocol=1,
        tool_protocol=negotiate(advertisement(catalog_digest())))
    async def withheld_delivery(identifier):
        return None
    monkeypatch.setattr(runtime,'deliver',withheld_delivery)
    async def run():
        admitted=asyncio.Event();loop=asyncio.get_running_loop();admit=runtime._admit_operation
        def observe(*args,**kwargs):
            receipt=admit(*args,**kwargs);loop.call_soon_threadsafe(admitted.set);return receipt
        monkeypatch.setattr(runtime,'_admit_operation',observe)
        async def revoke():
            await admitted.wait();runtime.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
        revocation=asyncio.create_task(revoke())
        try:
            with pytest.raises(DevError) as caught:
                await runtime.dispatch('fs_read',{'project':'project-team','path':'a','idempotency_key':'pending-then-revoke'},project,principal)
            assert caught.value.code=='SPACE_FORBIDDEN'
            assert original('SELECT id FROM operations WHERE id=?',(caught.value.details['operation_id'],))
        finally:
            if not revocation.done():revocation.cancel()
            await asyncio.gather(revocation,return_exceptions=True)
    asyncio.run(run())


def populate_native(app, owner='alice'):
    sid=uuid.uuid4().hex;store=app.state.store
    principal=Principal('panel:'+owner,owner,{'read','write','execute'},[],space_id='team')
    project=app.state.runtime.project('project-team',principal)
    store.execute('INSERT INTO native_ownership(id,space_id,owner_user_id,project_id,device_id,created) VALUES(?,?,?,?,?,?)',
        (sid,'team',owner,project['id'],project['device_id'],time.time()))
    events=[{'type':'message','text':'FIRST_PRIVATE_TEXT','receipt':'one'},
            {'type':'message','text':'SECOND_PRIVATE_TEXT','receipt':'two'}]
    raw=b''.join((json.dumps(event)+'\n').encode() for event in events)
    with closing(database(app.state.runtime.native.directory)) as db,db:
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (sid,project['id'],project['device_id'],project['root'],project['root'],'codex','Private','exited',1,1,'chat'))
        db.execute('INSERT INTO output VALUES(?,?,?)',(sid,0,raw))
    return sid


@pytest.mark.parametrize('format',['json','md'])
def test_native_export_rechecks_between_items_not_just_batches(team, format):
    app,b=team;set_member(b['owner'],'alice',project_policy('read','write','execute'));sid=populate_native(app)
    endpoint=next(r.endpoint for r in make_native_router(app.state.auth,app.state.runtime).routes if r.path.endswith('/export'))
    async def run():
        response=await endpoint(sid,request_for(b['alice'],f'/api/native/sessions/{sid}/export',sid=sid),format)
        iterator=response.body_iterator
        try:
            await anext(iterator)
            assert 'FIRST_PRIVATE_TEXT' in await anext(iterator)
            app.state.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            with pytest.raises(DevError):await anext(iterator)
        finally:await iterator.aclose()
    asyncio.run(run())


def test_native_event_replay_rechecks_between_frames(team):
    app,b=team;set_member(b['owner'],'alice',project_policy('read','write','execute'));sid=populate_native(app)
    endpoint=next(r.endpoint for r in make_native_router(app.state.auth,app.state.runtime).routes if r.path.endswith('/events'))
    request=request_for(b['alice'],f'/api/native/sessions/{sid}/events',sid=sid)
    async def connected():return False
    request.is_disconnected=connected
    async def run():
        response=await endpoint(sid,request,0);iterator=response.body_iterator
        try:
            assert 'event: session' in await anext(iterator)
            assert 'FIRST_PRIVATE_TEXT' in await anext(iterator)
            app.state.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            frame=await anext(iterator)
            assert 'event: error' in frame and 'SECOND_PRIVATE_TEXT' not in frame
            with pytest.raises(StopAsyncIteration):await anext(iterator)
        finally:await iterator.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('concurrent',['login','provider_edit','user_epoch'])
def test_stale_userinfo_denial_does_not_disable_newer_identity(oidc, concurrent):
    app,b,fake,row,_=oidc;_,session=login(oidc);store=app.state.store
    identity=store.one('SELECT * FROM external_identities WHERE user_id=?',(session['user_id'],))
    fake.userinfo_subject='wrong-subject'
    def race():
        if concurrent=='login':store.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',(store.encrypt('{}'),identity['id']))
        elif concurrent=='provider_edit':store.execute('UPDATE oidc_providers SET version=version+1 WHERE id=?',(row['id'],))
        else:store.execute('UPDATE iam_users SET epoch=epoch+1 WHERE user_id=?',(session['user_id'],))
    fake.on_userinfo=race
    with pytest.raises(DevError):asyncio.run(app.state.oidc.sync_identity(identity['id']))
    assert store.one('SELECT enabled FROM external_identities WHERE id=?',(identity['id'],))['enabled']==1


def test_authoritative_refresh_rejection_suspends_access_without_deleting_login(oidc):
    app,b,fake,_,_=oidc;user,session=login(oidc);store=app.state.store
    identity=store.one('SELECT * FROM external_identities WHERE user_id=?',(session['user_id'],))
    secured=json.loads(store.decrypt(identity['upstream_tokens']));secured['expires_at']=0
    store.execute('UPDATE external_identities SET upstream_tokens=? WHERE id=?',(store.encrypt(json.dumps(secured)),identity['id']))
    fake.fail_refresh=True
    assert asyncio.run(app.state.oidc.sync_identity(identity['id']))=='OIDC_REAUTH_REQUIRED'
    current=store.one('SELECT * FROM external_identities WHERE id=?',(identity['id'],))
    assert current['enabled']==1 and current['fresh_until']==0
    assert user.get('/api/session').json()['authenticated'] is True
    assert user.get('/api/projects').status_code==403
    assert store.one('SELECT id_hash FROM sessions WHERE id_hash=?',(session['id_hash'],))


def test_queued_mcp_request_rechecks_direct_membership_before_reconnect(team):
    app,b=team;policy=project_policy('read','write','execute');set_member(b['owner'],'alice',policy)
    profile=make_profile(b['owner'],policy);grant=make_connection(b['alice'],profile,live=True)
    receipt=data(call(b['alice'],grant['token'],'exec',{'project':'same-alias','command':'echo fixture',
        'idempotency_key':'queued-offline-member','yield_seconds':0}))
    row=app.state.store.one('SELECT * FROM operations WHERE id=?',(receipt['operation_id'],))
    assert app.state.runtime.permission_error(row) is None
    app.state.store.execute("UPDATE memberships SET policy='{}',version=version+1 WHERE user_id='alice' AND space_id='team'")
    assert app.state.runtime.permission_error(row) is not None


def test_inventory_enrollment_obeys_owner_account_not_ambient_space_membership(team):
    import re
    from types import SimpleNamespace
    app,b=team;store=app.state.store
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    ticket=must(b['alice'].post('/api/devices/device-team/install-ticket',json={
        'platform':'posix','hub_url':'https://fixture.invalid','allow_root':'/tmp/disposable'}))
    match=re.search(r'--token\s+(rdi_[A-Za-z0-9_-]{43})',ticket['command']);assert match
    connection=SimpleNamespace(device_secret=store.one("SELECT secret FROM devices WHERE id='device-team'")['secret'])
    assert app.state.runtime.connection_authorized('device-team',connection)
    must(b['owner'].put('/api/spaces/team/members/alice/suspension',json={'blocked':True}))
    # A workspace suspension changes its resource-use authority, not ownership
    # of the inventory device which could serve another authorized workspace.
    assert app.state.runtime.connection_authorized('device-team',connection)
    assert b['alice'].get('/api/projects/project-team').status_code==403
    store.execute("UPDATE iam_users SET active=0 WHERE user_id='alice'")
    assert not app.state.runtime.connection_authorized('device-team',connection)
    denied=b['alice'].post('/agent/enroll',headers={'Authorization':'Bearer '+match.group(1)})
    assert denied.status_code==401 and 'secret' not in denied.json()
    store.execute("UPDATE iam_users SET active=1 WHERE user_id='alice'")
    accepted=b['alice'].post('/agent/enroll',headers={'Authorization':'Bearer '+match.group(1)})
    assert accepted.status_code==200
    assert store.one("SELECT space_id,owner_user_id FROM audit WHERE action='device.enrolled' ORDER BY id DESC LIMIT 1")=={
        'space_id':None,'owner_user_id':'alice'}
