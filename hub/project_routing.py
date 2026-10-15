"""One resolver for logical projects, shared NFS locations and native actions.

Routing is not authorization. Every chosen route additionally requires project
permission, route permission, consent ceilings and node-local filesystem gates.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from hub import iam
from hub.gateway.catalog import fingerprint
from hub.routing_policy import allows
from shared.execution_hooks import plan_hooks
from shared.routing_contracts import FAMILIES, SpaceExecutionSettings, RoutedExecution, mount_path
from shared.util import DevError


GIT_TOOLS = frozenset({'git_status', 'git_diff', 'git_log', 'worktrees_create', 'worktrees_list', 'worktrees_remove'})


def family_for(tool, args):
    if tool == 'routed_job_control':
        return 'test'
    if tool in {'exec', 'routed_exec'}:
        return 'ssh' if args.get('target', 'agent') != 'agent' else args.get('action', 'shell')
    if tool in GIT_TOOLS:
        return 'git'
    if tool in {'shell_exec', 'tasks_run', 'tasks_list', 'execution_info', 'validation_run'}:
        return 'shell'
    if tool in {'ssh_exec', 'vps_exec'}:
        return 'ssh'
    if tool.startswith('browser_'):
        return 'browser'
    if tool.startswith('computer_'):
        return 'computer'
    if tool == 'native':
        return 'native'
    return 'files'


def settings(store, project):
    space = store.one('SELECT * FROM space_execution_settings WHERE space_id=?', (project['space_id'],))
    local = store.one('SELECT * FROM project_execution_settings WHERE project_id=?', (project['id'],))
    return (SpaceExecutionSettings.model_validate(json.loads(space['settings']) if space else {}),
            SpaceExecutionSettings.model_validate(json.loads(local['settings']) if local else {}), space, local)


def configured_route(store, project, family, selected=None):
    if not project.get('routing_managed'):
        return None
    if not project.get('source_root_id'):
        raise DevError('PROJECT_SOURCE_UNBOUND', 'Project has no source binding; its identity and external integrations are retained', 409)
    origin = 'explicit'
    if not selected:
        binding = store.one('SELECT * FROM project_action_routes WHERE project_id=? AND family=?', (project['id'], family))
        if binding is not None:
            origin, selected = 'project', binding['route_id']
            if selected is None:
                raise DevError('PROJECT_ROUTE_DISABLED', 'This action is explicitly disabled for the project', 403)
        else:
            binding = store.one('SELECT * FROM space_route_defaults WHERE space_id=? AND family=?', (project['space_id'], family))
            origin, selected = 'space', binding['route_id'] if binding else None
    if not selected:
        raise DevError('ACTION_ROUTE_MISSING', 'No route is configured for ' + family, 409)
    route = store.one('SELECT * FROM action_routes WHERE id=? AND space_id=?', (selected, project['space_id']))
    if not route or not route['enabled'] or route['family'] != family:
        raise DevError('ACTION_ROUTE_UNAVAILABLE', 'The configured route is disabled, removed or has a different action', 409)
    source = store.one('SELECT * FROM source_roots WHERE id=? AND space_id=?', (project['source_root_id'], project['space_id']))
    mount = store.one('SELECT * FROM source_mounts WHERE source_root_id=? AND device_id=? AND space_id=?',
        (project['source_root_id'], route['device_id'], project['space_id']))
    device = store.one('SELECT * FROM devices WHERE id=? AND space_id=?', (route['device_id'], project['space_id']))
    if not source or not source['enabled'] or not mount or not mount['enabled']:
        raise DevError('SOURCE_MOUNT_UNAVAILABLE', 'This shared source has no enabled mount on the selected device', 409)
    if not iam.device_identity_active(store, device):
        raise DevError('ROUTE_DEVICE_DISABLED', 'The selected device identity is not active', 403)
    space, local, space_row, local_row = settings(store, project)
    plan = plan_hooks(family, json.loads(route['settings']), space.hooks, local.hooks)
    # Target, execution policy and source identity are security-relevant. Device
    # heartbeat/hostname changes are not a reason to invalidate a client grant.
    digest = fingerprint([project['id'], project['alias'], project['routing_version'], project['mode'],
        project['allow_tasks'], project['source_root_id'], project['subpath'],
        source['id'], source['version'], mount['id'], mount['version'], mount['path'], mount['mode'], mount['allow_tasks'],
        route, space_row, local_row, plan])
    return {'route': route, 'mount': mount, 'device': device, 'source': source, 'plan': plan,
        'root': mount_path(mount['path'], project['subpath']), 'origin': origin, 'hash': digest,
        'space_settings': space, 'project_settings': local}


def fixed_route_snapshot(store, policy, space_id):
    result = {}
    for rule in policy.route_rules:
        if rule.all_projects:
            raise ValueError('Fixed route policy must enumerate project IDs')
        for project_id in rule.projects:
            project = store.one('SELECT * FROM projects WHERE id=? AND space_id=?', (project_id, space_id))
            route = store.one('SELECT family FROM action_routes WHERE id=? AND space_id=?', (rule.route_id, space_id))
            if not project or not route or not project.get('routing_managed'):
                continue
            try:
                resolved = configured_route(store, project, route['family'], rule.route_id)
            except DevError:
                continue
            result[fingerprint([project_id, rule.route_id])] = resolved['hash']
    return result


class ProjectRouting:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def require(self, principal, project, selected):
        from hub.roles import connection_policy, _policy
        principal = iam.live_principal(self.store, principal)
        if project['space_id'] != principal.space_id:
            raise DevError('PROJECT_NOT_FOUND', 'Project is not in the authenticated space', 404)
        if principal.admin and not principal.grant_id:
            return
        identifier = selected['route']['id']
        if principal.grant_id:
            grant = self.store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
            policy = connection_policy(self.store, grant)
            permitted = policy is not None and allows(policy, identifier, project['id'])
            if permitted and grant.get('authorization_mode') != 'role':
                ceiling = json.loads(grant.get('route_ceiling') or '{}')
                if ceiling.get(fingerprint([project['id'], identifier])) != selected['hash']:
                    raise DevError('ROUTE_FIXED_CONSENT_CHANGED', 'This route or source is outside the fixed connection target snapshot; review a new connection', 403)
        else:
            permitted = any(r['enabled'] and allows(_policy(r), identifier, project['id'])
                for r in iam.assigned_roles(self.store, principal.user_id, principal.space_id))
        if not permitted:
            raise DevError('ROUTE_POLICY_DENIED', 'The project/action route has not been explicitly granted', 403)

    def select(self, project, tool, args, principal, *, family=None):
        if not project.get('routing_managed'):
            if tool in {'exec', 'routed_exec'} and (args.get('action', 'shell') != 'shell' or args.get('argv')):
                raise DevError('PROJECT_ROUTING_REQUIRED', 'Adopt this project into shared-source routing before selecting a test/Git action', 409)
            return project
        if args.get('workspace_id'):
            raise DevError('ROUTED_WORKSPACE_UNSUPPORTED', 'Managed worktree IDs remain node-local; use the shared project source view for routed actions', 409)
        family = family or family_for(tool, args)
        selected = configured_route(self.store, project, family)
        self.require(principal, project, selected)
        mount, route, plan = selected['mount'], selected['route'], selected['plan']
        if plan['provider'] == 'rch' and tool not in {'exec', 'routed_exec', 'routed_job_control', 'project_tests_list', 'project_routes_get'}:
            raise DevError('RCH_EXPLICIT_TEST_REQUIRED', 'Use exec with action=test and direct argv or a configured test command', 409)
        context = {'route_id': route['id'], 'route_hash': selected['hash'], 'family': family,
            'source_root_id': project['source_root_id'], 'subpath': project['subpath'],
            'device_id': route['device_id'], 'mount_id': mount['id']}
        return {**project, 'device_id': route['device_id'], 'device_name': selected['device']['name'],
            'device_enabled': selected['device']['enabled'], 'root': selected['root'],
            'mode': 'write' if project['mode'] == mount['mode'] == 'write' else 'read',
            'allow_tasks': bool(project['allow_tasks'] and mount['allow_tasks']),
            '_route_context': context, '_execution_plan': plan,
            '_source_boundary': {'mount': mount['path'], 'subpath': project['subpath'], 'protocol': 1}}

    def prepare_execution(self, name, args, project):
        if not project.get('routing_managed'):
            if name == 'exec':
                if args.get('argv') or args.get('action', 'shell') != 'shell':
                    raise DevError('PROJECT_ROUTING_REQUIRED', 'Explicit Git/test routing requires adopting this project into shared-source management', 409)
                return name, {k: v for k, v in args.items() if k not in {'action', 'argv'}}
            return name, args
        if name != 'exec' or args.get('target', 'agent') != 'agent':
            return name, args
        values = dict(args)
        if values.get('action') == 'test' and values.get('task'):
            space, local, _, _ = settings(self.store, project)
            recipes = {r.name: r for r in space.tests}
            recipes.update({r.name: r for r in local.tests})
            recipe = recipes.get(values['task'])
            if not recipe:
                raise DevError('TEST_COMMAND_NOT_FOUND', 'The test command is not configured in this project or space', 404)
            if values.get('env') or values.get('cwd', '.') != '.':
                raise DevError('TEST_COMMAND_OVERRIDE', 'Configured test commands use their reviewed cwd and arguments', 400)
            values.update(argv=recipe.argv, command='', task='', cwd=recipe.cwd,
                          timeout_seconds=min(values.get('timeout_seconds', 3600), recipe.timeout_seconds))
        return 'routed_exec', RoutedExecution.model_validate(values).model_dump()

    def summary(self, project, principal):
        rows = []
        for family in FAMILIES:
            try:
                selected = configured_route(self.store, project, family)
                self.require(principal, project, selected)
                rows.append({'action': family, 'configured': True, 'authorized': True,
                    'route_id': selected['route']['id'], 'name': selected['route']['name'],
                    'device_id': selected['device']['id'], 'device_name': selected['device']['name'],
                    'root': selected['root'], 'source': selected['origin'],
                    'provider': selected['plan']['provider'], 'hooks': selected['plan']['hooks'],
                    'online': self.runtime.online(selected['device']['id'])})
            except DevError as exc:
                rows.append({'action': family, 'configured': exc.code not in {'ACTION_ROUTE_MISSING', 'PROJECT_SOURCE_UNBOUND'},
                    'authorized': False, 'online': False, 'error_code': exc.code})
        return rows

    def hydrate(self, project, principal):
        if not project.get('routing_managed'):
            return project
        routes = self.summary(project, principal)
        files = next(r for r in routes if r['action'] == 'files')
        return {**project, 'device_id': files.get('device_id'), 'device_name': files.get('device_name', ''),
            'device_enabled': int(files.get('authorized', False)), 'root': files.get('root'),
            'execution_routes': routes, 'source_bound': bool(project.get('source_root_id'))}

    def revalidate(self, op, request, caller):
        if not request['project'].get('_route_context'):
            return None
        project = self.store.one('SELECT * FROM projects WHERE id=?', (op['project_id'],))
        if not project or not project.get('routing_managed'):
            return 'The original logical project was removed or changed'
        try:
            current = self.select(project, op['tool'], request['args'], caller)
        except DevError as exc:
            return exc.message
        if (current.get('_route_context') != request['project']['_route_context']
                or current['root'] != request['project']['root'] or current['device_id'] != op['device_id']):
            return 'Source, route, hooks or execution device changed; the original operation was not redirected'
        return None

    def before_admission(self, project, name, args, caller):
        if not project.get('id'):
            return project, name, args
        current = self.store.one('SELECT * FROM projects WHERE id=? AND space_id=?', (project['id'], caller.space_id))
        if current and current.get('routing_managed'):
            selected = self.select(current, name, args, caller)
            if project.get('_route_context') and project['_route_context'] != selected['_route_context']:
                raise DevError('ACTION_ROUTE_CHANGED', 'The route changed during admission; no operation was submitted', 409)
            if name == 'routed_job_control':
                self.authorize_job_control(selected, args, caller)
            name, args = self.prepare_execution(name, args, selected)
            return selected, name, args
        if project.get('_route_context'):
            raise DevError('ACTION_ROUTE_CHANGED', 'The logical project changed before admission', 409)
        name, args = self.prepare_execution(name, args, project)
        return project, name, args

    def source_blocker(self, op):
        if not op.get('route_context') or op.get('attempts') or op['tool'] == 'routed_job_control':
            return None
        context = json.loads(op['route_context'])
        rows = self.store.all("""SELECT id,created,tool,state,route_context FROM operations
            WHERE space_id=? AND id<>? AND source_lock_released=0 AND route_context IS NOT NULL
            AND (created<? OR (created=? AND id<?))
            AND state IN ('queued','running','reconnecting','cancelling','needs_review') ORDER BY created,id""",
            (op['space_id'], op['id'], op['created'], op['created'], op['id']))
        from shared.contracts import MUTATING, PROCESS_TOOLS
        for row in rows:
            if row['tool'] == 'routed_job_control':
                continue
            other = json.loads(row['route_context'])
            if context['source_root_id'] != other['source_root_id']:
                continue
            a, b = PurePosixPath(context['subpath']), PurePosixPath(other['subpath'])
            if not (a == b or a in b.parents or b in a.parents):
                continue
            if row['state'] == 'needs_review' or op['tool'] in MUTATING | PROCESS_TOOLS or row['tool'] in MUTATING | PROCESS_TOOLS:
                return row['id']
        return None

    @staticmethod
    def require_protocol(connection):
        if getattr(connection, 'project_routing_protocol', 0) != 1:
            raise DevError('AGENT_ROUTING_UPGRADE_REQUIRED', 'This device must support project routing protocol 1 before handling shared-source actions', 409)

    def require_receipt(self, operation, principal):
        """Reading a receipt does not select a new target or restore old authority."""
        context = operation.get('route_context')
        if not context:
            return
        context = json.loads(context)
        from hub.roles import connection_policy, _policy
        principal = iam.live_principal(self.store, principal)
        if principal.admin and not principal.grant_id:
            return
        route_id, project_id = context['route_id'], operation['project_id']
        if principal.grant_id:
            grant = self.store.one('SELECT * FROM grants WHERE id=?', (principal.grant_id,))
            policy = connection_policy(self.store, grant)
            permitted = policy is not None and allows(policy, route_id, project_id)
            if permitted and grant.get('authorization_mode') != 'role':
                ceiling = json.loads(grant.get('route_ceiling') or '{}')
                permitted = ceiling.get(fingerprint([project_id, route_id])) == context['route_hash']
        else:
            permitted = any(r['enabled'] and allows(_policy(r), route_id, project_id)
                for r in iam.assigned_roles(self.store, principal.user_id, principal.space_id))
        if not permitted:
            raise DevError('ROUTE_RECEIPT_DENIED', 'Current project-route permission is required to read the original operation receipt', 403)

    def authorize_job_control(self, project, args, principal):
        original = self.runtime.operation_row(args['original_operation_id'], principal)
        if (original['tool'] != 'routed_exec' or original['project_id'] != project['id']
                or original['device_id'] != project['device_id'] or not original.get('route_context')
                or json.loads(original['route_context']) != project.get('_route_context')):
            raise DevError('RCH_ORIGINAL_SCOPE', 'RCH recovery must use the original caller, project and route', 403)
        if original['grant_id'] != principal.grant_id or original['owner_user_id'] != principal.user_id:
            raise DevError('RCH_ORIGINAL_SCOPE', 'RCH job control is private to the original caller and connection', 403)

    def complete_control(self, operation, data):
        if operation['tool'] != 'routed_job_control' or not data.get('command_ok'):
            return
        if (data.get('rch') or {}).get('terminal_acknowledged') is not True:
            return
        original = self.store.one('SELECT * FROM operations WHERE id=?', (data.get('original_operation_id'),))
        if (not original or original['project_id'] != operation['project_id'] or original['device_id'] != operation['device_id']
                or original['grant_id'] != operation['grant_id'] or original['owner_user_id'] != operation['owner_user_id']
                or original['route_context'] != operation['route_context']):
            return
        self.store.execute('UPDATE operations SET source_lock_released=1 WHERE id=?', (original['id'],))
        self.runtime.wake_delivery()

    def tests(self, project, principal):
        if not project.get('routing_managed'):
            return {'tests': [], 'note': 'Adopt the project into shared-source routing to configure space/project test commands.'}
        selected = self.select(project, 'project_tests_list', {}, principal, family='test')
        space, local, _, _ = settings(self.store, project)
        recipes = {r.name: {**r.model_dump(), 'source': 'space'} for r in space.tests}
        recipes.update({r.name: {**r.model_dump(), 'source': 'project'} for r in local.tests})
        return {'tests': list(recipes.values()), 'route': selected['_route_context'],
            'provider': selected['_execution_plan']['provider'],
            'invocation': {'tool': 'exec', 'action': 'test', 'task': '<name>'}}
