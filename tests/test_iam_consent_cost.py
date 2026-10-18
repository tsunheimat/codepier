"""Current member/Profile/Connection consent is linear and rechecks authority.

No Role or arbitrary fixed-grant API is retained. OAuth follows the same two-stage
space selection as the UI; policy edits and connection issuance are distinct.
"""
from __future__ import annotations

import uuid

import pytest

from hub import permissions
from tests.identity_fixtures import (team, must, set_member, make_profile,
    pending_oauth, select_oauth_space, project_policy)  # noqa: F401
from tests.test_iam_review_regressions import projects


def prepare(browsers, kind, selected):
    alice=browsers['alice']
    policy={'project_rules':[{'actions':['read'],'projects':selected}]}
    if kind=='profile':
        return lambda:alice.post('/api/spaces/team/profiles',json={
            'label':'New profile '+uuid.uuid4().hex,'policy':policy,'idempotency_key':uuid.uuid4().hex})
    if kind=='member':
        members=must(browsers['owner'].get('/api/spaces/team/members'))['members']
        version=next(m['version'] for m in members if m['user_id']=='bob' and m['source']=='manual')
        return lambda:alice.put('/api/spaces/team/members/bob',json={
            'policy':policy,'expected_version':version,'idempotency_key':uuid.uuid4().hex})
    profile=make_profile(browsers['owner'],policy)
    if kind=='connection':
        return lambda:alice.post('/api/spaces/team/connections',json={
            'name':'Consent cost fixture','profile_id':profile['id'],'profile_version':profile['version'],
            'consent_mode':'fixed','idempotency_key':uuid.uuid4().hex})
    pending=pending_oauth(alice)
    selected_space=select_oauth_space(alice,pending)
    return lambda:alice.post('/api/oauth/requests/'+pending['id']+'/decide',json={
        'allow':True,'space_id':'team','profile_id':profile['id'],'profile_version':profile['version'],
        'selection_version':selected_space['selection_version']})


def full_membership(browsers):
    set_member(browsers['owner'],'alice',{
        'project_rules':[{'all_projects':True,'actions':['read']}],'manage':['members','profiles']})


@pytest.mark.parametrize('kind',['profile','member','connection','oauth'])
def test_bulk_consent_evaluates_workspace_policy_linearly(team, monkeypatch, record_property, kind):
    app,browsers=team; store=app.state.store
    full_membership(browsers)
    original=permissions.project_actions
    evaluations=[]
    def counting(*args,**kwargs):
        evaluations.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(permissions,'project_actions',counting)
    def measured(count):
        projects(store,count)
        selected=['project-team',*[f'load-{n}' for n in range(count)]]
        submit=prepare(browsers,kind,selected)
        evaluations.clear()
        response=submit()
        assert response.status_code in (200,201),response.text
        return len(evaluations)
    small,large=measured(10),measured(250)
    record_property('policy_evaluations_11_projects',small)
    record_property('policy_evaluations_251_projects',large)
    assert small>0 and large>0
    # A bounded number of full maps, never a full map per selected project.
    assert large<=small*24+251
    assert large<=15*251


@pytest.mark.parametrize('kind',['profile','member','connection','oauth'])
def test_pending_consent_cannot_reuse_membership_before_its_revocation(team, kind):
    app,browsers=team; full_membership(browsers)
    assert prepare(browsers,kind,['project-team'])().status_code in (200,201)
    pending=prepare(browsers,kind,['project-team'])
    app.state.store.execute("UPDATE memberships SET active=0 WHERE user_id='alice' AND space_id='team'")
    response=pending()
    assert response.status_code==403,response.text


@pytest.mark.parametrize('kind',['profile','member'])
def test_one_unauthorized_project_rejects_entire_policy_edit(team, kind):
    app,browsers=team; projects(app.state.store,10)
    set_member(browsers['owner'],'alice',project_policy('read',manage=('profiles','members')))
    before_profiles=app.state.store.all('SELECT * FROM access_profiles')
    before_members=app.state.store.all("SELECT * FROM memberships WHERE user_id='bob'")
    response=prepare(browsers,kind,['project-team','load-0'])()
    assert response.status_code==403,response.text
    assert app.state.store.all('SELECT * FROM access_profiles')==before_profiles
    assert app.state.store.all("SELECT * FROM memberships WHERE user_id='bob'")==before_members
    assert not app.state.store.all("SELECT id FROM grants WHERE user_id='alice'")
