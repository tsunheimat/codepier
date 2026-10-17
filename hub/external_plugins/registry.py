"""Human-managed integration configuration and explicit per-grant consent."""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field

from hub import iam
from hub.db_worker import database_endpoint
from hub.gateway import catalog
from hub.external_plugins.contracts import Contract, validate_value
from hub.external_plugins.service import reviewed_dependency
from shared.util import DevError


class BackendInput(Contract):
    binding_id: str = Field(pattern=r'^gwb_[a-f0-9]{32}$')
    tools: dict[str, str] = Field(min_length=1, max_length=32)


class IntegrationInput(Contract):
    plugin_id: str = Field(min_length=1, max_length=40)
    manifest_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    backends: dict[str, BackendInput] = Field(min_length=1, max_length=8)
    config: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=0)


class AttachmentInput(Contract):
    config: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=0)


class Toggle(Contract):
    expected_version: int = Field(ge=1)
    enabled: bool


class ConsentInput(Contract):
    confirmed: Literal[True]
    expected_profile_version: int = Field(ge=1)
    review_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


def public_integration(row):
    value = {k: row[k] for k in ('id', 'alias', 'plugin_id', 'manifest_hash', 'version', 'enabled')}
    value.update(config=json.loads(row['config']), backends=json.loads(row['backends']))
    return value


def pin_backends(store, principal, spec, supplied):
    if set(supplied) != set(spec.backend_slots):
        raise DevError('PLUGIN_BACKEND_INVALID', 'Configure exactly the declared backend slots', 400)
    resolved = {}
    for name, requirement in spec.backend_slots.items():
        backend = supplied[name]
        if set(backend.tools) != set(requirement.tool_slots):
            raise DevError('PLUGIN_BACKEND_INVALID', 'Configure exactly the declared tool slots', 400)
        tools = {}
        for slot, tool_name in backend.tools.items():
            plan = reviewed_dependency(store, principal, backend.binding_id, tool_name)
            tool = plan[-1]
            schema = tool['inputSchema']
            for action in spec.actions.values():
                if action.call.backend != name or action.call.tool_slot != slot:
                    continue
                fields = set(action.call.arguments)
                if (not set(schema.get('required', [])) <= fields or
                        schema.get('additionalProperties') is False and not fields <= set(schema.get('properties', {}))):
                    raise DevError('PLUGIN_BACKEND_INCOMPATIBLE', 'Plugin mapping does not match the reviewed backend arguments', 409)
            tools[slot] = {'name': tool_name, 'hash': catalog.fingerprint(tool)}
        resolved[name] = {'binding_id': backend.binding_id, 'tools': tools}
    return resolved


def make_router(auth, runtime):
    router, store, plugins = APIRouter(), runtime.store, runtime.gateway.plugins

    def admin(request, write=False):
        runtime.gateway.require_enabled()
        return iam.require_space_capability(store,auth.panel(request,write),'resources')

    def own_grant(request, identifier, write=False):
        principal = auth.panel(request, write)
        grant = store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?',
            (identifier, principal.user_id, principal.space_id))
        if not grant:
            raise DevError('PLUGIN_GRANT_NOT_FOUND', 'Client connection does not belong to this account', 404)
        iam.validate_grant(store, grant)
        if not grant.get('profile_id'):
            raise DevError('PLUGIN_CONNECTION_REQUIRED', 'Use a workspace/Profile connection with explicit external MCP consent', 403)
        return principal, grant, runtime.grant_principal(grant['id'])

    @router.get('/api/external-plugins')
    @database_endpoint(store)
    def catalog_view(request: Request):
        auth.panel(request)
        return {'api_version': 1, 'plugins': plugins.registry.catalog(), 'installation': 'deployment-owned'}

    @router.get('/api/spaces/{space_id}/external-integrations')
    @database_endpoint(store)
    def list_integrations(request: Request):
        principal = admin(request)
        rows = store.all('SELECT * FROM external_integrations WHERE space_id=? ORDER BY alias,id', (principal.space_id,))
        return {'integrations': [public_integration(r) for r in rows]}

    @router.put('/api/spaces/{space_id}/external-integrations/{alias}')
    @database_endpoint(store)
    def save_integration(alias: str, request: Request, body: IntegrationInput):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,19}', alias):
            raise DevError('PLUGIN_ALIAS_INVALID', 'Use a short lowercase integration alias', 400)
        principal = admin(request, True)
        with store.transaction():
            principal = admin(request, True)
            spec = plugins.registry.get(body.plugin_id)
            if catalog.fingerprint(spec.model_dump()) != body.manifest_hash:
                raise DevError('PLUGIN_REVIEW_REQUIRED', 'Read the current installed manifest before saving', 409)
            validate_value(spec.space_config_schema, body.config, 'PLUGIN_CONFIG_INVALID')
            backends = pin_backends(store, principal, spec, body.backends)
            old = store.one('SELECT * FROM external_integrations WHERE space_id=? AND alias=?', (principal.space_id, alias))
            if body.expected_version != (old['version'] if old else 0):
                raise DevError('PLUGIN_CONFIG_CHANGED', 'Integration changed; refresh before saving', 409)
            if old and old['plugin_id'] != spec.id:
                raise DevError('PLUGIN_IDENTITY_IMMUTABLE', 'Create another integration to change the plugin identity', 409)
            if old:
                identifier = old['id']
                store.db.execute('UPDATE external_integrations SET manifest_hash=?,manifest=?,backends=?,config=?,version=version+1 WHERE id=?',
                    (body.manifest_hash, catalog.encoded(spec.model_dump()), catalog.encoded(backends), catalog.encoded(body.config), identifier))
            else:
                if store.one('SELECT count(*) AS n FROM external_integrations WHERE space_id=?', (principal.space_id,))['n'] >= 64:
                    raise DevError('PLUGIN_LIMIT', 'Space integration limit reached', 409)
                identifier = 'ext_' + uuid.uuid4().hex
                store.db.execute('''INSERT INTO external_integrations
                    (id,space_id,owner_user_id,alias,plugin_id,manifest_hash,manifest,backends,config,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''', (identifier, principal.space_id, principal.user_id, alias,
                    spec.id, body.manifest_hash, catalog.encoded(spec.model_dump()), catalog.encoded(backends), catalog.encoded(body.config), time.time()))
            store.audit(principal.actor, 'plugin.integration_saved', identifier, detail={'plugin_id': spec.id}, commit=False)
            return public_integration(store.one('SELECT * FROM external_integrations WHERE id=?', (identifier,)))

    @router.patch('/api/spaces/{space_id}/external-integrations/{identifier}')
    @database_endpoint(store)
    def toggle_integration(identifier: str, request: Request, body: Toggle):
        principal = admin(request, True)
        with store.transaction():
            changed = store.db.execute('UPDATE external_integrations SET enabled=?,version=version+1 WHERE id=? AND space_id=? AND version=?',
                (int(body.enabled), identifier, principal.space_id, body.expected_version)).rowcount
            if not changed:
                raise DevError('PLUGIN_CONFIG_CHANGED', 'Integration is missing or changed', 409)
            store.audit(principal.actor, 'plugin.integration_toggled', identifier, detail={'enabled': body.enabled}, commit=False)
        return {'ok': True}

    @router.get('/api/spaces/{space_id}/external-integrations/{identifier}/projects')
    @database_endpoint(store)
    def attachments(identifier: str, request: Request):
        principal = admin(request)
        if not store.one('SELECT id FROM external_integrations WHERE id=? AND space_id=?', (identifier, principal.space_id)):
            raise DevError('PLUGIN_UNAVAILABLE', 'Integration not found', 404)
        rows = store.all('''SELECT pi.* FROM project_integrations pi JOIN projects p ON p.id=pi.project_id
            WHERE pi.integration_id=? AND p.space_id=? ORDER BY pi.project_id''', (identifier, principal.space_id))
        return {'attachments': [{**r, 'config': json.loads(r['config'])} for r in rows]}

    @router.put('/api/spaces/{space_id}/external-integrations/{identifier}/projects/{project_id}')
    @database_endpoint(store)
    def attach(identifier: str, project_id: str, request: Request, body: AttachmentInput):
        principal = admin(request, True)
        with store.transaction():
            _, spec = plugins.integration(identifier, principal.space_id)
            if not store.one('SELECT id FROM projects WHERE id=? AND space_id=?', (project_id, principal.space_id)):
                raise DevError('PLUGIN_PROJECT_DENIED', 'Project must belong to this space', 404)
            validate_value(spec.project_config_schema, body.config, 'PLUGIN_CONFIG_INVALID')
            old = store.one('SELECT * FROM project_integrations WHERE integration_id=? AND project_id=?', (identifier, project_id))
            if body.expected_version != (old['version'] if old else 0):
                raise DevError('PLUGIN_CONFIG_CHANGED', 'Project attachment changed; refresh before saving', 409)
            if old:
                store.db.execute('UPDATE project_integrations SET config=?,enabled=1,version=version+1 WHERE id=?', (catalog.encoded(body.config), old['id']))
            else:
                if store.one('SELECT count(*) AS n FROM project_integrations WHERE integration_id=?', (identifier,))['n'] >= 500:
                    raise DevError('PLUGIN_LIMIT', 'Integration attachment limit reached', 409)
                store.db.execute('INSERT INTO project_integrations(id,integration_id,project_id,config) VALUES(?,?,?,?)',
                    ('exp_' + uuid.uuid4().hex, identifier, project_id, catalog.encoded(body.config)))
            store.audit(principal.actor, 'plugin.project_attached', project_id, detail={'integration_id': identifier}, commit=False)
            row = store.one('SELECT * FROM project_integrations WHERE integration_id=? AND project_id=?', (identifier, project_id))
            return {**row, 'config': json.loads(row['config'])}

    @router.delete('/api/spaces/{space_id}/external-integrations/{identifier}/projects/{project_id}')
    @database_endpoint(store)
    def detach(identifier: str, project_id: str, request: Request, expected_version: int = Query(ge=1)):
        principal = admin(request, True)
        with store.transaction():
            if not store.one('SELECT id FROM external_integrations WHERE id=? AND space_id=?', (identifier, principal.space_id)):
                raise DevError('PLUGIN_UNAVAILABLE', 'Integration not found', 404)
            changed = store.db.execute('UPDATE project_integrations SET enabled=0,version=version+1 WHERE integration_id=? AND project_id=? AND version=?',
                (identifier, project_id, expected_version)).rowcount
            if not changed:
                raise DevError('PLUGIN_CONFIG_CHANGED', 'Attachment is missing or changed', 409)
            store.audit(principal.actor, 'plugin.project_detached', project_id, detail={'integration_id': identifier}, commit=False)
        return {'ok': True, 'resource_deleted': False}

    @router.get('/api/spaces/{space_id}/external-integrations/grants/{identifier}/consent')
    @database_endpoint(store)
    def preview(identifier: str, request: Request):
        _, grant, caller = own_grant(request, identifier)
        profile = store.one('SELECT version FROM access_profiles WHERE id=? AND space_id=?',(grant['profile_id'],grant['space_id']))
        if not profile:
            raise DevError('PLUGIN_PROFILE_CHANGED','Profile is no longer available', 409)
        approved = store.one('SELECT 1 AS ok FROM external_plugin_consents WHERE grant_id=?', (grant['id'],))
        return {**plugins.consent_preview(caller), 'profile_version':profile['version'], 'has_consent': bool(approved)}

    @router.post('/api/spaces/{space_id}/external-integrations/grants/{identifier}/consent')
    @database_endpoint(store)
    def consent(identifier: str, request: Request, body: ConsentInput):
        with store.transaction():
            principal, grant, caller = own_grant(request, identifier, True)
            profile=store.one('SELECT version,enabled FROM access_profiles WHERE id=? AND space_id=?',(grant['profile_id'],grant['space_id']))
            if not profile or not profile['enabled'] or profile['version']!=body.expected_profile_version:
                raise DevError('PLUGIN_PROFILE_CHANGED','Profile changed; review the current actions', 409)
            selection = plugins.consent_preview(caller)
            if not selection['snapshot'] or selection['review_hash'] != body.review_hash:
                raise DevError('PLUGIN_REVIEW_CHANGED', 'No eligible actions or the reviewed targets changed', 409)
            store.db.execute('''INSERT INTO external_plugin_consents(grant_id,space_id,user_id,profile_id,snapshot,created)
                VALUES(?,?,?,?,?,?) ON CONFLICT(grant_id) DO UPDATE SET space_id=excluded.space_id,
                user_id=excluded.user_id,profile_id=excluded.profile_id,
                snapshot=excluded.snapshot,created=excluded.created''', (grant['id'], grant['space_id'], grant['user_id'],
                grant['profile_id'], catalog.encoded(selection['snapshot']), time.time()))
            store.audit(principal.actor, 'plugin.actions_consented', grant['id'], detail={'review_hash': selection['review_hash']}, commit=False)
        return {'ok': True, 'review_hash': selection['review_hash'], 'follows_future_plugin_changes': False}

    @router.delete('/api/spaces/{space_id}/external-integrations/grants/{identifier}/consent')
    @database_endpoint(store)
    def revoke(identifier: str, request: Request):
        with store.transaction():
            principal, grant, _ = own_grant(request, identifier, True)
            store.db.execute('DELETE FROM external_plugin_consents WHERE grant_id=?', (grant['id'],))
            store.audit(principal.actor, 'plugin.consent_revoked', identifier, commit=False)
        return {'ok': True}

    return router
