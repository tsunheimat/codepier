"""Resolve a panel request from explicit resource IDs, never an ambient workspace.

MCP principals are already bound. This module cannot rebind them, and a resource
ID is a lookup key rather than permission: membership/policy are checked after it.
"""
from __future__ import annotations

from dataclasses import replace
from hub import iam
from shared.util import DevError

TABLES = frozenset({'projects','operations','artifacts','access_profiles','grants','space_sources',
                   'space_vps','execution_sessions','native_ownership','native_upload_ownership',
                   'conversations','gateway_calls','gateway_bindings'})


def in_resource(store, principal, table, identifier):
    if table not in TABLES:
        raise ValueError('Unknown resource context table')
    row = store.one('SELECT space_id FROM '+table+' WHERE id=?', (identifier,))
    if not row:
        raise DevError('RESOURCE_NOT_FOUND', 'The requested resource is unavailable', 404)
    space_id = row['space_id']
    if principal.space_id is not None or principal.grant_id:
        if principal.space_id != space_id:
            raise DevError('RESOURCE_NOT_FOUND', 'Resource is not in this connection’s workspace', 404)
        return iam.live_principal(store, principal)
    return iam.live_principal(store, replace(principal, space_id=space_id))


def project_context(store, principal, identifier):
    if principal.space_id is not None or principal.grant_id:
        return iam.live_principal(store, principal)
    # Panel controls use stable project IDs. Aliases only have meaning inside an
    # already identified workspace; never select an arbitrary same-named project.
    return in_resource(store, principal, 'projects', identifier)


def tool_context(store, principal, name, arguments):
    if principal.space_id is not None or principal.grant_id:
        return iam.live_principal(store, principal)
    selected = principal
    refs = []
    project = arguments.get('project')
    if project:
        refs.append(('projects', project))
    target = arguments.get('target', '')
    if isinstance(target, str) and target.startswith('vps:'):
        refs.append(('space_vps', target[4:]))
    if name == 'session' and arguments.get('session_id'):
        refs.append(('execution_sessions', arguments['session_id']))
    for key, table in [('operation_id','operations'),('artifact_id','artifacts'),('source_id','space_sources')]:
        if arguments.get(key):
            refs.append((table, arguments[key]))
    # Facades split batched IDs into concrete calls, each resolving its own
    # explicit record. A caller must not turn that into an ambient account scope.
    if len(arguments.get('operation_ids', [])) == 1:
        refs.append(('operations', arguments['operation_ids'][0]))
    for table, identifier in refs:
        selected = in_resource(store, selected, table, identifier)
    return selected
