"""Live workspace/Profile checks for one immutable binding, never actions x resources."""
from __future__ import annotations

import json

from hub import iam
from hub.access_profiles import refresh_profile_principal
from hub.permissions import connection_policy
from hub.gateway.catalog import fingerprint
from shared.util import DevError


def grant_context(store, principal):
    principal = refresh_profile_principal(store, principal)
    if not principal.grant_id:
        raise DevError('GATEWAY_CONNECTION_REQUIRED', '外部 MCP 需要明确同意的客户端连接', 403)
    grant = store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
    consent = store.one('SELECT * FROM gateway_consents WHERE grant_id=?', (principal.grant_id,))
    if not consent or consent['consent_version'] != 1 or any(consent[key] != grant[key] for key in ('user_id', 'space_id', 'profile_id')):
        raise DevError('GATEWAY_CONSENT_REQUIRED', '请由此凭据的账号在面板明确同意外部 MCP 委派', 403)
    policy = connection_policy(store, grant)
    return principal, policy


def accessible(account, principal):
    return account['space_id'] == principal.space_id and (account['sharing'] == 'space' or account['owner_user_id'] == principal.user_id)


def binding_rows(store, binding_id, space_id):
    binding = store.one('SELECT * FROM gateway_bindings WHERE id=? AND space_id=?', (binding_id, space_id))
    account = store.one('SELECT * FROM gateway_accounts WHERE id=? AND space_id=?', (binding['account_id'], space_id)) if binding else None
    connector = store.one('SELECT * FROM gateway_connectors WHERE id=?', (account['connector_id'],)) if account else None
    if not binding or not account or not connector:
        raise DevError('GATEWAY_TOOL_NOT_FOUND', '工具或连接不存在于当前授权范围', 404)
    return binding, account, connector


def within_fixed_consent(store, principal, binding_id, tool):
    grant = store.one('SELECT consent_mode,connector_ceiling FROM grants WHERE id=?', (principal.grant_id,))
    if grant['consent_mode'] == 'live':
        return True
    try:
        return json.loads(grant['connector_ceiling'])[binding_id].get(tool['name']) == fingerprint(tool)
    except (ValueError, TypeError, KeyError):
        return False


def require_tool(store, principal, binding_id, tool_name):
    principal, policy = grant_context(store, principal)
    if not any(rule.binding_id == binding_id and tool_name in rule.tools for rule in policy.connector_rules):
        raise DevError('GATEWAY_POLICY_DENIED', 'The workspace Profile does not allow this tool/binding pair', 403)
    binding, account, connector = binding_rows(store, binding_id, principal.space_id)
    if not accessible(account, principal) or not all(row['enabled'] for row in (binding, account, connector)):
        raise DevError('GATEWAY_POLICY_DENIED', '连接已暂停、移除或不属于此账号', 403)
    tool = next((tool for tool in json.loads(binding['tools']) if tool['name'] == tool_name), None)
    if tool is None:
        raise DevError('GATEWAY_TOOL_NOT_FOUND', '工具未获批准发布', 404)
    if not within_fixed_consent(store, principal, binding_id, tool):
        raise DevError('GATEWAY_FIXED_CONSENT_CHANGED', '工具定义超出此固定连接的初次同意；请核对并建立新的明确连接', 403)
    current = next((item for item in json.loads(account['catalog']) if item['name'] == tool_name), None)
    if current is None or fingerprint(current) != fingerprint(tool):
        raise DevError('GATEWAY_SCHEMA_REVIEW_REQUIRED', '发现的工具定义已改变；请审核并重新发布', 409)
    return principal, binding, account, connector, tool


def visible_tools(store, principal):
    try:
        principal, policy = grant_context(store, principal)
    except DevError:
        return []
    visible, seen = [], set()
    # Bound by AccessPolicy (32 rules) and gateway catalog limits. No cross-await
    # permission cache or backend discovery request is performed by tools/list.
    for rule in policy.connector_rules:
        try:
            binding, account, connector = binding_rows(store, rule.binding_id, principal.space_id)
        except DevError:
            continue
        if not accessible(account, principal) or not all(row['enabled'] for row in (binding, account, connector)):
            continue
        discovered = {tool['name']: fingerprint(tool) for tool in json.loads(account['catalog'])}
        for tool in json.loads(binding['tools']):
            key = (binding['id'], tool['name'])
            if (tool['name'] in rule.tools and key not in seen and discovered.get(tool['name']) == fingerprint(tool)
                    and within_fixed_consent(store, principal, binding['id'], tool)):
                seen.add(key); visible.append((binding, tool))
    return visible


def consent_grant(store, principal, grant_id, expected_profile_version):
    """Explicit downstream consent, bound to the new connection identity."""
    import time
    store.require_transaction()
    grant = store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?',
                      (grant_id,principal.user_id,principal.space_id))
    if not grant:
        raise DevError('CONNECTION_NOT_FOUND','Connection not found',404)
    iam.validate_grant(store,grant)
    connection_policy(store,grant)
    profile = store.one('SELECT * FROM access_profiles WHERE id=? AND space_id=?',(grant['profile_id'],grant['space_id']))
    if not profile or not profile['enabled'] or profile['version']!=expected_profile_version:
        raise DevError('PROFILE_CHANGED','The Profile changed; review its permissions again',409)
    store.db.execute("""INSERT INTO gateway_consents(grant_id,user_id,space_id,profile_id,consent_version,created)
        VALUES(?,?,?,?,1,?) ON CONFLICT(grant_id) DO UPDATE SET consent_version=1,created=excluded.created""",
        (grant_id,principal.user_id,principal.space_id,grant['profile_id'],time.time()))
    store.audit(principal.actor,'gateway.delegation_consented',grant_id,
        detail={'profile_id':profile['id'],'profile_version':profile['version'],'live_profile':grant['consent_mode']=='live'},commit=False)
