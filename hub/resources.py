"""Resource details collect configuration, access and existing private records."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field

from hub import iam
from hub.access import AccessModel
from hub.db_worker import database_endpoint
from hub.roles import _policy, created_ids, project_actions, vps_actions
from hub.gateway.policy import binding_rows, accessible
from shared.conversation_contracts import Conversations
from shared.util import DevError


class ProjectMCPAssociations(AccessModel):
    binding_ids: list[str] = Field(max_length=100)
    expected_binding_ids: list[str] = Field(max_length=100)


def make_resources_router(auth, runtime):
    router, store = APIRouter(), runtime.store

    @router.get('/api/resources/{kind}/{identifier}')
    @database_endpoint(store)
    def detail(kind: Literal['project', 'vps', 'mcp'], identifier: str, request: Request):
        principal = auth.panel(request)
        resource = runtime.conversations.resource(kind, identifier, principal)
        identifier = resource['id']
        associations = []
        if kind == 'project':
            configuration = runtime.project_public(runtime.project(identifier, principal))
            for row in runtime.vps.list({'project': identifier, 'offset': 0, 'limit': 200}, principal)['vps']:
                associations.append({'type': 'vps', 'id': row['id'], 'name': row['name']})
            for row in store.all('SELECT binding_id FROM project_mcp_resources WHERE project_id=?', (identifier,)):
                try:
                    associations.append(runtime.conversations.resource('mcp', row['binding_id'], principal))
                except DevError:
                    continue
        elif kind == 'vps':
            configuration = runtime.vps.get(identifier, principal)
            associations = [{'type': 'project', 'id': row['id'], 'name': row['alias']} for row in configuration['projects']]
        else:
            binding, account, connector = binding_rows(store, identifier, principal.space_id)
            configuration = {'id': binding['id'], 'name': connector['label'], 'endpoint': connector['endpoint'],
                'enabled': bool(binding['enabled'] and account['enabled'] and connector['enabled']),
                'binding': binding['alias'], 'account': account['label'], 'sharing': account['sharing'],
                'approved_tools': [t['name'] for t in json.loads(binding['tools'])]}
            for row in store.all('SELECT project_id FROM project_mcp_resources WHERE binding_id=?', (identifier,)):
                try:
                    associations.append(runtime.conversations.resource('project', row['project_id'], principal))
                except DevError:
                    continue
        roles = store.all('SELECT * FROM access_roles WHERE space_id=? ORDER BY label_key', (principal.space_id,)) if principal.admin else iam.assigned_roles(store, principal.user_id, principal.space_id)
        access = []
        for role in roles:
            policy = _policy(role)
            actions = (project_actions(policy, identifier, created_ids(store, role['id'])) if kind == 'project'
                       else vps_actions(policy, identifier) if kind == 'vps'
                       else {t for rule in policy.connector_rules if rule.binding_id == identifier for t in rule.tools})
            if actions:
                access.append({'role_id': role['id'], 'role': role['label'], 'enabled': bool(role['enabled']),
                               'actions' if kind != 'mcp' else 'tools': sorted(actions)})
        conversations = runtime.conversations.invoke(Conversations(resource_type=kind, resource_id=identifier).model_dump(), principal)
        operations = []
        if kind == 'mcp':
            operations = [{**r, 'type': 'mcp'} for r in store.all('''SELECT id,tool,state,created,updated FROM gateway_calls
                WHERE space_id=? AND user_id=? AND binding_id=? ORDER BY created DESC LIMIT 50''', (principal.space_id, principal.user_id, identifier))]
        else:
            clause, values = iam.private_sql(principal)
            clause += ' AND ' + ('project_id=?' if kind == 'project' else "json_extract(args_summary,'$.target')=?")
            values.append(identifier if kind == 'project' else 'vps:' + identifier)
            for row in store.all('SELECT id FROM operations WHERE ' + clause + ' ORDER BY created DESC LIMIT 50', values):
                try:
                    op = runtime.operation_row(row['id'], principal)
                    operations.append({'type': 'native', **{k: op[k] for k in ('id', 'tool', 'state', 'created', 'updated')}})
                except DevError:
                    continue
        targets = [identifier, resource['name'], *[op['id'] for op in operations]]
        marks = ','.join('?' for _ in targets)
        audit = store.all(f'''SELECT id,at,action,target,status FROM audit WHERE space_id=? AND owner_user_id=?
            AND (target IN ({marks}) OR json_extract(detail,'$.operation_id') IN ({marks}))
            ORDER BY id DESC LIMIT 50''', (principal.space_id, principal.user_id, *targets, *targets))
        return {'resource': resource, 'configuration': configuration, 'access': access,
                'associations': associations, 'association_grants_access': False,
                'conversations': conversations['conversations'], 'next_conversation_offset': conversations['next_offset'],
                'operations': operations, 'audit': audit, 'record_limit': 50}

    @router.put('/api/projects/{identifier}/mcp-services')
    @database_endpoint(store)
    def associate(identifier: str, request: Request, body: ProjectMCPAssociations):
        with store.transaction():
            principal = auth.admin(request, True)
            runtime.project(identifier, principal)
            existing = {r['binding_id'] for r in store.all('SELECT binding_id FROM project_mcp_resources WHERE project_id=?', (identifier,))}
            if existing != set(body.expected_binding_ids):
                raise DevError('RESOURCE_ASSOCIATIONS_CHANGED', '关联已变化，请重新读取', 409)
            target = set(body.binding_ids)
            if len(target) != len(body.binding_ids):
                raise DevError('INVALID_RESOURCE', '关联 ID 不可重复')
            for binding_id in target:
                _, account, _ = binding_rows(store, binding_id, principal.space_id)
                if not accessible(account, principal):
                    raise DevError('RESOURCE_NOT_FOUND', '不能关联其他人的私有 MCP 帐户', 404)
            store.db.execute('DELETE FROM project_mcp_resources WHERE project_id=?', (identifier,))
            for binding_id in target:
                store.db.execute('INSERT INTO project_mcp_resources VALUES (?,?)', (identifier, binding_id))
            iam.audit(store, principal, 'project.mcp_associated', identifier,
                      detail={'binding_ids': sorted(target), 'grants_access': False})
        return {'binding_ids': sorted(target), 'grants_access': False}

    return router
