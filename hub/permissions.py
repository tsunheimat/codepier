"""Direct workspace-member and profile policies; there is no Role indirection.

Actions are intersected for each concrete workspace resource, never as a Cartesian
product of independent action and resource lists. A connection owns its identity.
"""
from __future__ import annotations

import json
from functools import reduce
from typing import Literal

from pydantic import Field, model_validator

from hub.external_plugins.rules import PluginRule, intersect as intersect_plugins
from shared.workspace_contracts import (WorkspaceModel, ProjectRule, DeviceRule, FolderRule,
                                        ConnectorRule, VPSRule, CAPABILITIES)
from shared.util import DevError


class AccessPolicy(WorkspaceModel):
    manage: list[Literal['workspace', 'members', 'profiles', 'resources', 'connections', 'audit']] = Field(default_factory=list, max_length=6)
    project_rules: list[ProjectRule] = Field(default_factory=list, max_length=1000)
    device_rules: list[DeviceRule] = Field(default_factory=list, max_length=500)
    folder_rules: list[FolderRule] = Field(default_factory=list, max_length=500)
    connector_rules: list[ConnectorRule] = Field(default_factory=list, max_length=500)
    vps_rules: list[VPSRule] = Field(default_factory=list, max_length=500)
    plugin_rules: list[PluginRule] = Field(default_factory=list, max_length=256)

    @model_validator(mode='after')
    def distinct(self):
        if len(self.manage) != len(set(self.manage)):
            raise ValueError('Management capabilities must be distinct')
        if sum(len(r.actions) * max(1, len(r.projects)) for r in self.plugin_rules) > 256:
            raise ValueError('At most 256 explicit plugin action/resource pairs are supported')
        return self


def _policy(row):
    try:
        return AccessPolicy.model_validate(json.loads(row['policy']))
    except (ValueError, TypeError, KeyError) as exc:
        raise DevError('INVALID_POLICY', 'Workspace policy is invalid; no authority was inferred', 403) from exc


def project_actions(policy, project_id):
    return {action for rule in policy.project_rules
            if project_id not in rule.excluded_projects and (rule.all_projects or project_id in rule.projects)
            for action in rule.actions}


def vps_actions(policy, identifier):
    return {a for rule in policy.vps_rules if rule.all_vps or identifier in rule.vps for a in rule.actions}


def device_actions(policy, identifier):
    return {a for rule in policy.device_rules if identifier in rule.devices for a in rule.actions}


def folder_actions(policy, identifier):
    return {a for rule in policy.folder_rules if identifier in rule.folders for a in rule.actions}


def connector_tools(policy, identifier):
    return {t for rule in policy.connector_rules if rule.binding_id == identifier for t in rule.tools}


def member_policy(store, user_id, space_id):
    from hub import iam
    member = iam.membership(store, user_id, space_id)
    return AccessPolicy.model_validate(member['policy'])


def owner_policy():
    """The creator receives explicit direct rights; an account title grants none."""
    return AccessPolicy.model_validate({
        'manage': ['workspace', 'members', 'profiles', 'resources', 'connections', 'audit'],
        'project_rules': [{'all_projects': True, 'actions': sorted(CAPABILITIES)}],
        'vps_rules': [{'all_vps': True, 'actions': ['read', 'execute']}],
    })


def scoped_ids(store, space_id):
    return {
        'project': [r['id'] for r in store.all('SELECT id FROM projects WHERE space_id=?', (space_id,))],
        'device': [r['device_id'] for r in store.all('SELECT device_id FROM space_devices WHERE space_id=? AND enabled=1', (space_id,))],
        'folder': [r['id'] for r in store.all('SELECT id FROM space_folders WHERE space_id=? AND enabled=1', (space_id,))],
        'vps': [r['id'] for r in store.all('SELECT id FROM space_vps WHERE space_id=?', (space_id,))],
        'connector': [r['id'] for r in store.all('SELECT id FROM gateway_bindings WHERE space_id=?', (space_id,))],
    }


def intersect_policies(store, space_id, *policies):
    """Return a concrete, workspace-bounded policy for this authorization decision."""
    if not policies:
        return AccessPolicy()
    ids = scoped_ids(store, space_id)
    def actions(kind, getter):
        return [(identifier, reduce(set.intersection, (getter(p, identifier) for p in policies)))
                for identifier in ids[kind]]
    projects = [ProjectRule(projects=[i], actions=sorted(a)) for i, a in actions('project', project_actions) if 'read' in a]
    devices = [DeviceRule(devices=[i], actions=sorted(a)) for i, a in actions('device', device_actions) if a]
    folders = [FolderRule(folders=[i], actions=sorted(a)) for i, a in actions('folder', folder_actions) if 'read' in a]
    vps = [VPSRule(vps=[i], actions=sorted(a)) for i, a in actions('vps', vps_actions) if 'read' in a]
    connectors = [ConnectorRule(binding_id=i, tools=sorted(a)) for i, a in actions('connector', connector_tools) if a]
    plugins = policies[0].plugin_rules
    for policy in policies[1:]:
        plugins = intersect_plugins(AccessPolicy.model_construct(plugin_rules=plugins), policy)
    return AccessPolicy.model_construct(manage=[], project_rules=projects, device_rules=devices,
        folder_rules=folders, vps_rules=vps, connector_rules=connectors, plugin_rules=plugins)


def connection_binding(store, grant):
    from hub import iam
    if not grant or grant.get('revoked') or not grant.get('profile_id') or not grant.get('space_id'):
        raise DevError('INVALID_TOKEN', 'Connection is revoked or has no workspace/profile identity', 401)
    iam.validate_grant(store, grant)
    profile = store.one('SELECT * FROM access_profiles WHERE id=? AND space_id=?',
                        (grant['profile_id'], grant['space_id']))
    if not profile:
        raise DevError('INVALID_TOKEN', 'The immutable connection profile no longer exists', 401)
    if grant['consent_mode'] not in {'fixed', 'live'}:
        raise DevError('INVALID_TOKEN', 'Invalid connection consent mode', 401)
    return profile


def connection_policy(store, grant):
    profile = connection_binding(store, grant)
    if not profile['enabled']:
        return AccessPolicy()
    policies = [member_policy(store, grant['user_id'], grant['space_id']), _policy(profile)]
    if grant['consent_mode'] == 'fixed':
        snapshot = _policy({'policy': grant['resource_policy']})
        if any(r.all_projects for r in snapshot.project_rules) or any(r.all_vps for r in snapshot.vps_rules):
            raise DevError('INVALID_TOKEN', 'A fixed connection cannot contain future-resource selectors', 401)
        policies.append(snapshot)
    return intersect_policies(store, grant['space_id'], *policies)


def principal_grant(store, principal):
    if not principal.grant_id:
        return None
    grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
    if not grant or any(grant[k] != getattr(principal, k) for k in ('user_id', 'space_id', 'profile_id')):
        raise DevError('INVALID_TOKEN', 'Connection identity does not match its authenticated binding', 401)
    return grant


def principal_policy(store, principal):
    grant = principal_grant(store, principal)
    return connection_policy(store, grant) if grant else member_policy(store, principal.user_id, principal.space_id)


def project_scopes(store, principal, project_id):
    from hub import iam
    iam.project_in_space(store, principal, project_id)
    return project_actions(principal_policy(store, principal), project_id)


def require_access(store, principal, action, *, project_id=None, device_id=None, folder_id=None, creation=None):
    from hub import iam
    principal = iam.live_principal(store, principal)
    policy = principal_policy(store, principal)
    if action in {'get_profile', 'get_access_context'}:
        return policy
    if project_id is not None:
        iam.project_in_space(store, principal, project_id)
        allowed = project_actions(policy, project_id)
    elif folder_id is not None:
        row = store.one('SELECT id FROM space_folders WHERE id=? AND space_id=? AND enabled=1', (folder_id, principal.space_id))
        allowed = folder_actions(policy, folder_id) if row else set()
        if not principal.grant_id and ('workspace' in policy.manage or 'resources' in policy.manage) and row:
            allowed |= {'read', 'projects.create'}
    elif device_id is not None:
        row = store.one('SELECT device_id FROM space_devices WHERE device_id=? AND space_id=? AND enabled=1', (device_id, principal.space_id))
        allowed = device_actions(policy, device_id) if row else set()
        if not principal.grant_id and ('workspace' in policy.manage or 'resources' in policy.manage) and row:
            allowed |= {'devices.read'}
    elif action == 'read':
        return policy  # Discovery only; each returned resource is checked separately.
    else:
        allowed = set()
    if action not in allowed:
        raise DevError('WORKSPACE_POLICY_DENIED', 'This connection/member cannot perform that action on this workspace resource', 403)
    return policy


def validate_policy(store, space_id, policy):
    """Prevent configuration from naming inventory resources outside the workspace."""
    available = {k: set(v) for k, v in scoped_ids(store, space_id).items()}
    selections = {
        'project': {i for r in policy.project_rules for i in r.projects + r.excluded_projects},
        'device': {i for r in policy.device_rules for i in r.devices},
        'folder': {i for r in policy.folder_rules for i in r.folders},
        'vps': {i for r in policy.vps_rules for i in r.vps},
        'connector': {r.binding_id for r in policy.connector_rules},
    }
    for kind, ids in selections.items():
        if not ids <= available[kind]:
            raise DevError('RESOURCE_OUTSIDE_WORKSPACE', 'The policy selects a '+kind+' resource not provided by this workspace', 403)
    for rule in policy.connector_rules:
        binding = store.one('SELECT tools FROM gateway_bindings WHERE id=? AND space_id=?', (rule.binding_id, space_id))
        if not set(rule.tools) <= {t['name'] for t in json.loads(binding['tools'])}:
            raise DevError('MCP_TOOL_NOT_APPROVED', 'Select only reviewed tools in this workspace binding', 403)
    from hub.external_plugins.rules import validate_rules
    validate_rules(store, policy.plugin_rules, space_id)
    return policy


def policy_capabilities(policy):
    scopes = {'read'}
    for rule in policy.project_rules:
        scopes.update(rule.actions)
    for rule in policy.device_rules:
        scopes.update(rule.actions)
    for rule in policy.folder_rules:
        scopes.update(rule.actions)
    if any('execute' in r.actions for r in policy.vps_rules):
        scopes.add('execute')
    return scopes
