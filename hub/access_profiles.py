"""Profiles belong to workspaces; each MCP connection is its own stable identity."""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import replace

from fastapi import APIRouter, Request
from pydantic import Field

from hub import iam
from hub.db_worker import database_endpoint
from hub.permissions import (AccessPolicy, _policy, connection_binding, connection_policy,
    member_policy, intersect_policies, policy_capabilities, validate_policy)
from hub.workspace_mutations import label_key, mutate, version
from shared.workspace_contracts import WorkspaceModel
from shared.util import DevError


@iam.read_decision
def effective_grant(store, grant):
    profile=connection_binding(store,grant)
    policy=connection_policy(store,grant)
    projects=sorted({i for r in policy.project_rules if 'read' in r.actions for i in r.projects})
    return policy_capabilities(policy),projects,profile


@iam.read_decision
def refresh_profile_principal(store, principal):
    principal=iam.live_principal(store,principal)
    if not principal.grant_id:
        return principal
    grant=store.one('SELECT * FROM grants WHERE id=?',(principal.grant_id,))
    if not grant or any(grant[k]!=getattr(principal,k) for k in ('user_id','space_id','profile_id')):
        raise DevError('INVALID_TOKEN','Connection binding does not match its authenticated identity',401)
    scopes,projects,_=effective_grant(store,grant)
    return replace(principal,scopes=scopes,projects=projects,admin=False,instance_admin=False)


def validate_profile_consent(store, principal, profile_id, profile_version):
    principal=iam.live_principal(store,principal)
    if principal.grant_id or not principal.space_id:
        raise DevError('WORKSPACE_REQUIRED','Choose a workspace before choosing a Profile',400)
    profile=store.one('SELECT * FROM access_profiles WHERE id=? AND space_id=?',(profile_id,principal.space_id))
    if not profile or not profile['enabled']:
        raise DevError('PROFILE_NOT_FOUND','Choose an enabled Profile in this workspace',404)
    version(profile,profile_version)
    validate_policy(store,principal.space_id,_policy(profile))
    return profile


def current_profile(store, principal):
    principal=refresh_profile_principal(store,principal)
    if not principal.grant_id:
        raise DevError('TOKEN_REQUIRED','Use an authenticated MCP connection',401)
    grant=store.one('SELECT * FROM grants WHERE id=?',(principal.grant_id,))
    scopes,projects,profile=effective_grant(store,grant)
    return {'id':grant['id'],'name':'CodePier','nickname':grant['label']},scopes,projects,profile


def access_context(store, principal):
    identity,scopes,projects,profile=current_profile(store,principal)
    visible=[r for r in store.all('SELECT id,alias FROM projects WHERE space_id=? ORDER BY alias_key',(principal.space_id,)) if r['id'] in projects]
    return {'connection':identity,'profile':{'id':profile['id'],'label':profile['label']},
            'scopes':sorted(scopes),'projects':visible,'isolation':'connection',
            'note':'Resources are resolved by this connection. Use normal project tools; execution locations are optional and user-requested.'}


def public_profile(row):
    return {k:row[k] for k in ('id','label','version','created','updated','created_by')} | {
        'enabled':bool(row['enabled']),'policy':_policy(row).model_dump()}


class ProfileFields(WorkspaceModel):
    label: str=Field(min_length=1,max_length=80)
    policy: AccessPolicy
    enabled: bool=True


class ProfileCreate(ProfileFields):
    idempotency_key: str=Field(min_length=8,max_length=128)


class ProfileUpdate(ProfileFields):
    expected_version: int=Field(ge=1)
    idempotency_key: str=Field(min_length=8,max_length=128)


def editable_policy(store, principal, policy):
    if policy.manage:
        raise DevError('PROFILE_ADMIN_POLICY','Profiles configure resource use; they do not grant workspace administration')
    validate_policy(store,principal.space_id,policy)
    if not principal.admin:
        own=member_policy(store,principal.user_id,principal.space_id)
        requested=intersect_policies(store,principal.space_id,policy)
        effective=intersect_policies(store,principal.space_id,own,policy)
        if requested.model_dump()!=effective.model_dump() or any(r.all_projects for r in policy.project_rules) or any(r.all_vps for r in policy.vps_rules):
            raise DevError('DELEGATION_EXCEEDED','A profile manager cannot grant resources/actions beyond their own direct rights',403)
    return policy


def make_profiles_router(auth,runtime):
    router,store=APIRouter(),runtime.store

    def authorize_replay(current,receipt):
        iam.require_space_capability(store,current,'profiles')
        if not store.one('SELECT 1 FROM access_profiles WHERE id=? AND space_id=?',(receipt['id'],current.space_id)):
            raise DevError('PROFILE_NOT_FOUND','Profile not found',404)

    @router.get('/api/spaces/{space_id}/profiles')
    @database_endpoint(store)
    def profiles(request:Request):
        principal=auth.panel(request)
        iam.membership(store,principal.user_id,principal.space_id)
        return {'profiles':[public_profile(r) for r in store.all('SELECT * FROM access_profiles WHERE space_id=? ORDER BY label_key,id',(principal.space_id,))]}

    @router.get('/api/spaces/{space_id}/profiles/{profile_id}')
    @database_endpoint(store)
    def read_profile(profile_id:str,request:Request):
        principal=auth.panel(request)
        row=store.one('SELECT * FROM access_profiles WHERE id=? AND space_id=?',(profile_id,principal.space_id))
        if not row:
            raise DevError('PROFILE_NOT_FOUND','Profile not found',404)
        return public_profile(row)

    @router.post('/api/spaces/{space_id}/profiles')
    @database_endpoint(store)
    def create_profile(body:ProfileCreate,request:Request):
        principal=iam.require_space_capability(store,auth.panel(request,True),'profiles')
        def save(current):
            iam.require_space_capability(store,current,'profiles')
            editable_policy(store,current,body.policy)
            label,key=label_key(body.label);now=time.time();identifier='prf_'+uuid.uuid4().hex
            try:
                store.db.execute('INSERT INTO access_profiles(id,space_id,label,label_key,policy,created_by,enabled,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                    (identifier,current.space_id,label,key,body.policy.model_dump_json(),current.user_id,int(body.enabled),now,now))
            except sqlite3.IntegrityError as exc:
                raise DevError('PROFILE_NAME_CONFLICT','This workspace already has a Profile with that name',409) from exc
            return public_profile(store.one('SELECT * FROM access_profiles WHERE id=?',(identifier,)))
        return mutate(store,principal,'profile.create',body.idempotency_key,body.model_dump(),save,
                      authorize_replay=authorize_replay)

    @router.put('/api/spaces/{space_id}/profiles/{profile_id}')
    @database_endpoint(store)
    def update_profile(profile_id:str,body:ProfileUpdate,request:Request):
        principal=iam.require_space_capability(store,auth.panel(request,True),'profiles')
        def save(current):
            iam.require_space_capability(store,current,'profiles')
            row=store.one('SELECT * FROM access_profiles WHERE id=? AND space_id=?',(profile_id,current.space_id))
            version(row,body.expected_version)
            editable_policy(store,current,body.policy)
            label,key=label_key(body.label)
            try:
                store.db.execute('UPDATE access_profiles SET label=?,label_key=?,policy=?,enabled=?,version=version+1,updated=? WHERE id=?',
                    (label,key,body.policy.model_dump_json(),int(body.enabled),time.time(),profile_id))
            except sqlite3.IntegrityError as exc:
                raise DevError('PROFILE_NAME_CONFLICT','This workspace already has a Profile with that name',409) from exc
            runtime.wake_delivery()
            return public_profile(store.one('SELECT * FROM access_profiles WHERE id=?',(profile_id,)))
        return mutate(store,principal,'profile.update:'+profile_id,body.idempotency_key,body.model_dump(),save,
                      authorize_replay=authorize_replay)
    return router
