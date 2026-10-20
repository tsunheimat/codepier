"""Live workspace privacy, with no ambient space header or old stream events."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import Request

from hub.api.activity import make_activity_router
from hub.api.context import HubContext
from tests.identity_fixtures import team, set_member, project_policy  # noqa: F401
from tests.current_model import CurrentModel


def event_endpoint(app):
    context=HubContext(app.state.store,app.state.runtime,app.state.auth,
        app.state.config,app.state.runtime.panel_maintenance,
        lambda:'http://testserver',Path(__file__).resolve().parents[1])
    return next(route.endpoint for route in make_activity_router(context).routes if route.path=='/api/events')


def event_request(browser):
    request=Request({'type':'http','method':'GET','path':'/api/events','query_string':b'access_events=2',
        'headers':[(b'cookie',('rd_session='+browser.cookie).encode())]})
    async def connected():return False
    request.is_disconnected=connected
    return request


@pytest.mark.parametrize('revocation',['membership','session','member_disabled','member_policy','expiry','project_delete'])
def test_live_stream_signals_authority_loss_without_private_payload(team, revocation):
    app,b=team;store=app.state.store;set_member(b['owner'],'alice',project_policy('read','execute'))
    async def run():
        response=await event_endpoint(app)(event_request(b['alice']));stream=response.body_iterator
        try:
            assert 'event: ready' in await anext(stream)
            if revocation=='membership':store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            elif revocation=='session':store.execute("DELETE FROM sessions WHERE user_id='alice'")
            elif revocation=='expiry':store.execute("UPDATE sessions SET expires=1 WHERE user_id='alice'")
            elif revocation=='member_disabled':store.execute("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'")
            elif revocation=='project_delete':store.execute("DELETE FROM projects WHERE id='project-team'")
            else:store.execute("UPDATE memberships SET policy=?,version=version+1 WHERE user_id='alice' AND space_id='team'",
                               (json.dumps(project_policy('read')),))
            for queue in app.state.runtime.watchers:
                queue.put_nowait({'type':'iam','data':{'space_id':'team','user_id':'alice','private':'PRIVATE_SENTINEL'}})
            event='session_revoked' if revocation in {'session','expiry'} else 'access_changed'
            assert await anext(stream)==f'event: {event}\ndata: {{}}\n\n'
            with pytest.raises(StopAsyncIteration):await anext(stream)
        finally:await stream.aclose()
        assert not app.state.runtime.watchers
    asyncio.run(run())
    assert b['alice'].get('/api/session').json()['authenticated']==(revocation not in {'session','expiry'})


def test_global_audit_export_enumerates_authorized_workspaces_without_a_current_space(team):
    app,b=team;store=app.state.store
    set_member(b['owner'],'alice',project_policy('read'))
    personal=store.one("SELECT id FROM spaces WHERE kind='personal' AND owner_user_id='owner'")['id']
    for space in ('team','other-team',personal):
        store.execute('''INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id)
            VALUES(1,'panel:owner',?,'','ok','{}',?,'owner')''',('OWNER_SENTINEL_'+space,space))
    store.execute('''INSERT INTO audit(at,actor,action,target,status,detail,space_id,owner_user_id)
        VALUES(1,'panel:alice','ALICE_PRIVATE_SENTINEL','','ok','{}','team','alice')''')
    own=b['owner'].get('/api/audit-export')
    assert own.status_code==200
    assert all('OWNER_SENTINEL_'+space in own.text for space in ('team','other-team',personal))
    assert 'ALICE_PRIVATE_SENTINEL' not in own.text
    alice=b['alice'].get('/api/audit-export')
    assert alice.status_code==200 and 'ALICE_PRIVATE_SENTINEL' in alice.text
    assert 'OWNER_SENTINEL_' not in alice.text
    # A query string is not a principal workspace selector; it can never expand
    # which records the authenticated account is allowed to export.
    query=b['alice'].get('/api/audit-export?space_id=other-team')
    assert query.status_code==200 and 'OWNER_SENTINEL_' not in query.text
    header=b['alice'].get('/api/audit-export',headers={'X-CodePier-Space':'other-team'})
    assert header.status_code==400 and 'SENTINEL' not in header.text
    assert own.headers['cache-control']=='no-store'
    assert 'current_space_id' not in b['owner'].get('/api/iam/me').text


def test_idle_heartbeat_rechecks_membership_without_invalidating_benign_rename(team):
    app,b=team;set_member(b['owner'],'alice',project_policy('read'))
    async def run():
        response=await event_endpoint(app)(event_request(b['alice']));stream=response.body_iterator
        try:
            await anext(stream);queue=next(iter(app.state.runtime.watchers))
            app.state.store.execute("UPDATE spaces SET label='Harmless rename' WHERE id='team'")
            queue.put_nowait(None)
            assert await anext(stream)==': heartbeat\n\n'
            app.state.store.execute("INSERT INTO membership_blocks VALUES('team','alice',1)")
            queue.put_nowait(None)
            assert await anext(stream)=='event: access_changed\ndata: {}\n\n'
        finally:await stream.aclose()
    asyncio.run(run())
    assert b['alice'].get('/api/session').json()['authenticated']


def test_new_project_and_newly_granted_access_preserve_stream_and_login(team):
    app,b=team;store=app.state.store;set_member(b['owner'],'alice',project_policy('read'))
    original=store.all("SELECT * FROM sessions WHERE user_id='alice'")
    async def run():
        response=await event_endpoint(app)(event_request(b['alice']));stream=response.body_iterator
        try:
            assert 'event: ready' in await anext(stream)
            CurrentModel(store).project('future-project',space='team',folder='folder-team',owner='owner',alias='Future',subpath='future')
            policy={'project_rules':[{'projects':['project-team','future-project'],'actions':['read']}]}
            store.execute("UPDATE memberships SET policy=?,version=version+1 WHERE user_id='alice' AND space_id='team'",
                (json.dumps(policy),))
            next(iter(app.state.runtime.watchers)).put_nowait(None)
            assert await anext(stream)==': heartbeat\n\n'
        finally:await stream.aclose()
    asyncio.run(run())
    assert store.all("SELECT * FROM sessions WHERE user_id='alice'")==original
    assert b['alice'].get('/api/session').json()['authenticated']
    assert 'future-project' in {p['id'] for p in b['alice'].get('/api/projects').json()['projects']}
