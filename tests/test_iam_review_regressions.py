"""Direct-workspace IAM cost, cache invalidation, audit and stream regressions.

Removed Role-created-project/delegation and schema-upgrade behavior is not
emulated. Security/cost contracts operate on real current membership policies.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import time

import pytest

from hub import iam
from hub.iam_read import read_decision
from hub.runtime import Principal
from shared.util import DevError
from tests.identity_fixtures import (team, must, set_member, project_policy,
    make_profile, make_connection, call, data)  # noqa: F401


def principal(user='alice', space='team'):
    return Principal('panel:'+user, user, {'read'}, [], space_id=space)


def projects(store, count):
    with store.transaction():
        for n in range(count):
            store.db.execute('''INSERT OR IGNORE INTO projects
                (id,alias,alias_key,source_id,subpath,space_id,owner_user_id,created)
                VALUES(?,?,?,'folder-team',?,'team','owner',?)''',
                (f'load-{n}',f'load-{n}',f'load-{n}',f'load-{n}',time.time()))


_query_measurement = ContextVar('iam_query_measurement', default=None)


def queries(store, call):
    # Count only this request's statements, not the shared maintenance worker.
    statements=[]; marker=object(); token=_query_measurement.set(marker)
    def trace(sql):
        if _query_measurement.get() is marker: statements.append(sql)
    with store.lock: store.db.set_trace_callback(trace)
    try: result=call()
    finally:
        with store.lock: store.db.set_trace_callback(None)
        _query_measurement.reset(token)
    return result,[x for x in statements if x.lstrip().upper().startswith(('SELECT','WITH'))]


def allow(b, *actions):
    return set_member(b['owner'],'alice',project_policy(*(actions or ('read',))))


def remove_project_access(store):
    store.execute("UPDATE memberships SET policy='{}',version=version+1 WHERE user_id='alice' AND space_id='team'")


def test_live_principal_query_cost_does_not_scale_with_workspace_projects(team, record_property):
    app,b=team; store=app.state.store
    set_member(b['owner'],'alice',{'project_rules':[{'actions':['read','write'],'all_projects':True}]})
    projects(store,10)
    small,small_sql=queries(store,lambda:iam.live_principal(store,principal()))
    projects(store,250)
    large,large_sql=queries(store,lambda:iam.live_principal(store,principal()))
    record_property('selects_11_projects',len(small_sql))
    record_property('selects_251_projects',len(large_sql))
    assert small_sql and large_sql
    assert len(small.projects)==11 and len(large.projects)==251
    assert len(large_sql)<=len(small_sql)+1 and len(large_sql)<=20
    assert 'write' in large.scopes
    assert not any('role_assignments' in sql or 'access_roles' in sql for sql in large_sql)


def test_actual_device_endpoint_query_cost_not_multiplied_by_project_count(team, record_property):
    app,b=team; store=app.state.store; allow(b)
    store.execute("UPDATE devices SET owner_user_id='alice' WHERE id='device-team'")
    projects(store,10)
    small,small_sql=queries(store,lambda:b['alice'].get('/api/devices'))
    projects(store,250)
    large,large_sql=queries(store,lambda:b['alice'].get('/api/devices'))
    record_property('device_endpoint_selects_11_projects',len(small_sql))
    record_property('device_endpoint_selects_251_projects',len(large_sql))
    assert small.status_code==large.status_code==200
    assert small_sql and large_sql and len(large_sql)<=len(small_sql)+2
    assert len(large_sql)<45


def operation(store, identifier='review-op'):
    store.execute('''INSERT INTO operations
        (id,device_id,project_id,actor,tool,args_summary,fingerprint,state,created,updated,space_id,owner_user_id)
        VALUES(?,'device-team','project-team','panel:alice','fs_read','{}','','succeeded',?,?,'team','alice')''',
        (identifier,time.time(),time.time()))


def test_operation_read_reuses_snapshot_then_reloads_current_member_policy(team):
    app,b=team; store=app.state.store; allow(b); operation(store); projects(store,200)
    row,sql=queries(store,lambda:app.state.runtime.operation_row('review-op',principal()))
    assert row['id']=='review-op'
    assert sum('SELECT id FROM projects WHERE space_id=' in q for q in sql)<=1
    assert len(sql)<35
    remove_project_access(store)
    with pytest.raises(DevError): app.state.runtime.operation_row('review-op',principal())


@pytest.mark.parametrize('revocation',['policy','membership','profile','connection'])
def test_existing_connection_never_uses_a_previous_allow_decision(team, revocation):
    app,b=team; store=app.state.store; allow(b,'read','execute')
    profile=make_profile(b['owner'],project_policy('read','execute'))
    created=make_connection(b['alice'],profile,live=True)
    assert 'execute' in data(call(b['alice'],created['token']))['scopes']
    if revocation=='policy':
        remove_project_access(store)
        assert not data(call(b['alice'],created['token']))['projects']
    elif revocation=='membership':
        store.execute("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'")
        assert call(b['alice'],created['token']).status_code==403
    elif revocation=='profile':
        store.execute('UPDATE access_profiles SET enabled=0 WHERE id=?',(profile['id'],))
        paused=data(call(b['alice'],created['token']))
        assert paused['connection']['id']==created['connection_id']
        assert paused['projects']==[] and 'execute' not in paused['scopes']
        denied=call(b['alice'],created['token'],'exec',{'project':'project-team','command':'true',
            'idempotency_key':'paused-profile-command','yield_seconds':0})
        assert denied.json()['result']['isError'] is True
        assert not store.all("SELECT id FROM operations WHERE grant_id=?",(created['connection_id'],))
    else:
        must(b['alice'].post('/api/spaces/team/connections/'+created['connection_id']+'/revoke'))
        assert call(b['alice'],created['token']).status_code==401


def test_scope_invalidates_on_policy_write_within_the_scope(team):
    app,b=team; allow(b); store=app.state.store; p=principal()
    with iam.read_scope(store):
        assert iam.live_principal(store,p).projects
        remove_project_access(store)
        assert not iam.live_principal(store,p).projects
    assert not iam.live_principal(store,p).projects


def test_scheduled_task_cannot_inherit_request_snapshot_as_authority(team):
    app,b=team; allow(b); store=app.state.store; p=principal()
    async def later(): return iam.live_principal(store,p)
    @read_decision
    def schedule(store):
        assert iam.live_principal(store,p).projects
        return asyncio.create_task(later())
    async def run():
        task=schedule(store)
        remove_project_access(store)
        assert not (await task).projects
    asyncio.run(run())


@pytest.mark.parametrize('kind',['async','generator','async_generator'])
def test_read_scope_cannot_decorate_a_wait_or_stream(kind):
    async def coroutine(store): return None
    def generator(store): yield None
    async def async_generator(store): yield None
    with pytest.raises(TypeError):
        read_decision({'async':coroutine,'generator':generator,'async_generator':async_generator}[kind])


def test_each_stream_event_observes_current_policy(team):
    app,b=team; allow(b); store=app.state.store; operation(store)
    event={'type':'operation','data':{'id':'review-op'}}
    assert iam.event_visible(app.state.runtime,principal(),event)
    remove_project_access(store)
    assert not iam.event_visible(app.state.runtime,principal(),event)


def test_resource_action_pairs_do_not_flatten_across_projects(team):
    app,b=team; store=app.state.store; projects(store,3)
    set_member(b['owner'],'alice',{'project_rules':[
        {'actions':['read'],'all_projects':True},
        {'actions':['read','execute'],'projects':['load-0']},
        {'actions':['read','write'],'projects':['load-1']}]})
    with iam.read_scope(store):
        p=iam.live_principal(store,principal())
        assert iam.human_project_actions(store,p,'load-0')=={'read','execute'}
        assert iam.human_project_actions(store,p,'load-1')=={'read','write'}
        assert iam.human_project_actions(store,p,'load-2')=={'read'}
        assert iam.human_project_actions(store,p,'project-other-team')==set()


@pytest.mark.parametrize('target',['project-team','review-op','device-team'])
def test_failed_login_target_never_selects_a_workspace(team, target):
    app,b=team; operation(app.state.store)
    response=b['bob'].post('/api/login',json={'username':target,'password':'not-a-real-password'})
    assert response.status_code==401
    event=app.state.store.one("SELECT * FROM audit WHERE action='auth.login' AND actor='anonymous' ORDER BY id DESC LIMIT 1")
    assert event['target']==target and event['space_id'] is None and event['owner_user_id'] is None


def test_audit_literal_target_has_no_resource_lookup(team):
    app,_=team; store=app.state.store
    token=iam.audit_context.set(None)
    try:
        _,sql=queries(store,lambda:store.audit('anonymous','auth.login','project-team','failed'))
        assert not sql
        _,sql=queries(store,lambda:store.audit('hub','project.inspected','project-team',target_kind='project'))
        assert len(sql)==1 and 'FROM projects' in sql[0]
        assert store.one('SELECT space_id FROM audit ORDER BY id DESC LIMIT 1')['space_id']=='team'
    finally: iam.audit_context.reset(token)


def test_typed_target_never_overrides_scoped_actor(team):
    app,_=team; store=app.state.store
    token=iam.audit_context.set((store,'panel:alice','team','alice'))
    try:
        _,sql=queries(store,lambda:store.audit('panel:alice','read.denied','project-other-team','denied',target_kind='project'))
        row=store.one('SELECT * FROM audit ORDER BY id DESC LIMIT 1')
        assert not sql and row['space_id']=='team' and row['owner_user_id']=='alice'
    finally: iam.audit_context.reset(token)


@pytest.mark.parametrize('kind',['projects','users','unknown','projects; DROP TABLE users'])
def test_audit_target_kind_is_an_internal_allowlist(team, kind):
    with pytest.raises(ValueError): team[0].state.store.audit('hub','inspection','id',target_kind=kind)
