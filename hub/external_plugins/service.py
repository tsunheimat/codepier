"""Resolve plugin actions to immutable, reauthorized gateway calls.

No transport, secrets, raw-tool grant, or autonomous work is owned by a plugin.
"""
from __future__ import annotations

import json
import unicodedata

from hub.gateway import catalog, policy
from hub.external_plugins.contracts import Registry, map_arguments, public_definition, validate_value
from hub.external_plugins.rules import allows
from hub.permissions import require_access
from shared.util import DevError


def route_key(integration, action, project):
    return catalog.fingerprint([integration, action, project])


def reviewed_dependency(store, principal, binding_id, name):
    """Backend gate only. Invocation also requires separate plugin-action consent."""
    binding, account, connector = policy.binding_rows(store, binding_id, principal.space_id)
    if not policy.accessible(account, principal) or not all(r['enabled'] for r in (binding, account, connector)):
        raise DevError('PLUGIN_BACKEND_DENIED', 'Backend is disabled or inaccessible to this account', 403)
    tool = next((t for t in json.loads(binding['tools']) if t['name'] == name), None)
    current = next((t for t in json.loads(account['catalog']) if t['name'] == name), None)
    if tool is None or current is None or catalog.fingerprint(current) != catalog.fingerprint(tool):
        raise DevError('PLUGIN_BACKEND_REVIEW_REQUIRED', 'Backend tool must be discovered, reviewed and published', 409)
    return principal, binding, account, connector, tool


class ExternalPlugins:
    def __init__(self, gateway, registry=None):
        self.gateway, self.store = gateway, gateway.store
        self.registry = registry or Registry.installed()

    def integration(self, identifier, space_id):
        row = self.store.one('SELECT * FROM external_integrations WHERE id=? AND space_id=?', (identifier, space_id))
        if not row or not row['enabled']:
            raise DevError('PLUGIN_UNAVAILABLE', 'External integration is not available', 404)
        spec = self.registry.get(row['plugin_id'])
        if catalog.fingerprint(spec.model_dump()) != row['manifest_hash']:
            raise DevError('PLUGIN_REVIEW_REQUIRED', 'Installed plugin changed; review the integration again', 409)
        return row, spec

    def project_id(self, principal, value):
        if not isinstance(value, str) or not value or len(value) > 100:
            raise DevError('PLUGIN_PROJECT_REQUIRED', 'An authorized CodePier project is required', 400)
        key = unicodedata.normalize('NFKC', value).casefold()
        project = self.store.one('SELECT id FROM projects WHERE space_id=? AND (id=? OR alias_key=?)',
            (principal.space_id, value, key))
        if not project:
            raise DevError('PLUGIN_PROJECT_DENIED', 'Project is not accessible', 404)
        require_access(self.store, principal, 'read', project_id=project['id'])
        return project['id']

    def resolve(self, principal, integration_id, action_name, project_value='', *, consent=True):
        self.gateway.require_enabled()
        principal, effective = policy.grant_context(self.store, principal)
        row, spec = self.integration(integration_id, principal.space_id)
        action = spec.actions.get(action_name)
        if action is None:
            raise DevError('PLUGIN_ACTION_NOT_FOUND', 'Plugin action is not available', 404)
        if action.scope == 'space' and project_value:
            raise DevError('PLUGIN_SCOPE_INVALID', 'A space action cannot receive a project selector', 400)
        project = self.project_id(principal, project_value) if action.scope == 'project' else ''
        if not allows(effective, integration_id, action_name, project):
            raise DevError('PLUGIN_POLICY_DENIED', 'This action/resource pair is not granted', 403)
        attachment = None
        project_config = {}
        if project:
            attachment = self.store.one('SELECT * FROM project_integrations WHERE integration_id=? AND project_id=?', (row['id'], project))
            if not attachment or not attachment['enabled']:
                raise DevError('PLUGIN_PROJECT_UNBOUND', 'Project is not attached to this integration', 409)
            project_config = json.loads(attachment['config'])
            validate_value(spec.project_config_schema, project_config, 'PLUGIN_CONFIG_INVALID')
        space_config = json.loads(row['config'])
        validate_value(spec.space_config_schema, space_config, 'PLUGIN_CONFIG_INVALID')
        backend = json.loads(row['backends'])[action.call.backend]
        selected = backend['tools'][action.call.tool_slot]
        plan = reviewed_dependency(self.store, principal, backend['binding_id'], selected['name'])
        if catalog.fingerprint(plan[-1]) != selected['hash']:
            raise DevError('PLUGIN_BACKEND_REVIEW_REQUIRED', 'Mapped tool changed; review the integration', 409)
        # Pin the installed contract, target mapping and selected tool, not just a label.
        digest = catalog.fingerprint([row['id'], row['version'], row['manifest_hash'], row['backends'], row['config'],
            attachment, action_name, selected])
        context = {'integration_id': row['id'], 'action': action_name, 'project_id': project, 'route_hash': digest}
        if consent:
            grant = self.store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
            approved = self.store.one('SELECT * FROM external_plugin_consents WHERE grant_id=?', (principal.grant_id,))
            if (not approved or any(approved[k] != grant[k] for k in ('space_id', 'user_id', 'profile_id'))
                    or json.loads(approved['snapshot']).get(route_key(row['id'], action_name, project)) != digest):
                raise DevError('PLUGIN_CONSENT_REQUIRED', 'Review and explicitly consent to the current plugin action/target', 403)
        return {'context': context, 'plan': plan, 'action': action, 'integration': row,
                'space_config': space_config, 'project_config': project_config}

    def authorize_context(self, principal, context):
        if not isinstance(context, dict) or set(context) != {'integration_id', 'action', 'project_id', 'route_hash'}:
            raise DevError('PLUGIN_CONTEXT_INVALID', 'Invalid plugin invocation context', 403)
        resolved = self.resolve(principal, context['integration_id'], context['action'], context['project_id'])
        if resolved['context'] != context:
            raise DevError('PLUGIN_ROUTE_CHANGED', 'Plugin target/configuration changed; original call was not replayed', 409)
        return resolved['plan']

    def records(self, principal, *, consent=True):
        self.gateway.require_enabled()
        principal, effective = policy.grant_context(self.store, principal)
        seen = set()
        for rule in effective.plugin_rules:
            for project in rule.projects if rule.scope == 'project' else ['']:
                for name in rule.actions:
                    key = route_key(rule.integration_id, name, project)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        yield self.resolve(principal, rule.integration_id, name, project, consent=consent)
                    except DevError:
                        # No enumeration of inaccessible backend account details.
                        continue

    def tools(self, principal):
        tools = {}
        try:
            for record in self.records(principal):
                context, row = record['context'], record['integration']
                definition = public_definition(row['alias'], context['action'], record['action'], 'workspace')
                tools[definition['name']] = definition
        except DevError:
            return []
        return sorted(tools.values(), key=lambda t: t['name'])

    def prepare(self, principal, name, arguments):
        principal, effective = policy.grant_context(self.store, principal)
        for identifier in sorted({r.integration_id for r in effective.plugin_rules}):
            try:
                row, spec = self.integration(identifier, principal.space_id)
            except DevError:
                continue
            for action_name, action in spec.actions.items():
                definition = public_definition(row['alias'], action_name, action, 'workspace')
                if definition['name'] != name:
                    continue
                validate_value(definition['inputSchema'], arguments)
                resolved = self.resolve(principal, identifier, action_name, arguments.get('project', ''))
                inputs = {k: v for k, v in arguments.items() if k != 'project'}
                mapped = map_arguments(action, inputs, resolved['project_config'], resolved['space_config'])
                # The backend schema is untrusted; expensive validation belongs
                # to the gateway's bounded validation worker, not the DB thread.
                catalog.validate_arguments(resolved['plan'][-1], mapped, schema_check=False)
                return resolved['context'], mapped
        raise DevError('PLUGIN_ACTION_NOT_FOUND', 'Plugin action is not in the current grant', 404)

    async def call(self, principal, name, arguments, reauthenticate, request_key=None):
        def prepare():
            fresh = reauthenticate()
            if (fresh.space_id, fresh.user_id, fresh.grant_id) != (principal.space_id, principal.user_id, principal.grant_id):
                raise DevError('PLUGIN_IDENTITY_CHANGED', 'Caller identity changed', 403)
            return self.prepare(fresh, name, arguments)
        context, mapped = await self.store.run(prepare)
        result = await self.gateway._call(principal, name, mapped, reauthenticate, request_key, plugin_context=context)
        return {**result, '_meta': {**result.get('_meta', {}), 'codepier/plugin': context}}

    def consent_preview(self, principal):
        records = list(self.records(principal, consent=False))
        snapshot = {route_key(c['integration_id'], c['action'], c['project_id']): c['route_hash']
                    for r in records for c in [r['context']]}
        direct = policy.visible_tools(self.store, principal)
        routes = []
        for record in records:
            _, binding, account, _, tool = record['plan']
            routes.append({**record['context'], 'integration_alias': record['integration']['alias'],
                'plugin_id': record['integration']['plugin_id'], 'effect': record['action'].effect,
                'title': record['action'].title, 'project_config': record['project_config'],
                'space_config': record['space_config'], 'backend_binding_id': binding['id'],
                'backend_tool': tool['name'], 'backend_account': {'id': account['id'], 'label': account['label'],
                    'sharing': account['sharing'], 'owner_user_id': account['owner_user_id']},
                'broader_direct_access': [{'binding_id': b['id'], 'tool': t['name']}
                    for b, t in direct if b['account_id'] == account['id']]})
        return {'routes': routes, 'snapshot': snapshot,
                'review_hash': catalog.fingerprint(snapshot)}
