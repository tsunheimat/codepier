"""One workspace-source resolver for all projects and durable execution plans.

No route objects grant authority. A project references one workspace folder;
execution, recovery and node-local worktrees retain their selected source identity.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath

from hub import iam
from hub.permissions import require_access
from hub.workspace_sources import (source_views, select_view, resolve_shell, require_target_consent,
    target_fingerprint, fingerprint, execution_options)
from shared.execution_hooks import plan_hooks
from shared.routing_contracts import SpaceExecutionSettings, RoutedExecution
from shared.util import DevError

GIT_TOOLS=frozenset({'git_status','git_diff','git_log','worktrees_create','worktrees_list','worktrees_remove'})


def family_for(tool,args):
    if tool=='routed_job_control':
        return 'test'
    if tool in {'exec','routed_exec','session'}:
        return 'ssh' if args.get('target','agent')!='agent' else args.get('action','shell')
    if tool in GIT_TOOLS:
        return 'git'
    if tool in {'shell_exec','tasks_run','tasks_list','execution_info','validation_run'}:
        return 'shell'
    if tool.startswith('browser_'):
        return 'browser'
    if tool.startswith('computer_'):
        return 'computer'
    if tool=='native':
        return 'native'
    return 'files'


def settings(store,project):
    space=store.one('SELECT * FROM space_execution_settings WHERE space_id=?',(project['space_id'],))
    local=store.one('SELECT * FROM project_execution_settings WHERE project_id=?',(project['id'],))
    try:
        return (SpaceExecutionSettings.model_validate(json.loads(space['settings']) if space else {}),
                SpaceExecutionSettings.model_validate(json.loads(local['settings']) if local else {}),space,local)
    except (ValueError,TypeError) as exc:
        raise DevError('EXECUTION_SETTINGS_INVALID','Stored execution settings cannot be applied safely',409) from exc


class ProjectRouting:
    def __init__(self,runtime):
        self.runtime,self.store=runtime,runtime.store

    def _worktree_device(self,project,args,principal):
        identifier=args.get('workspace_id') or args.get('target_workspace_id')
        if not identifier:
            return None
        original=self.runtime.operation_row(identifier,principal)
        if (original['tool']!='worktrees_create' or original['project_id']!=project['id']
                or original['grant_id']!=principal.grant_id or original['owner_user_id']!=principal.user_id):
            raise DevError('WORKTREE_ORIGINAL_SCOPE','A worktree belongs to its original project, device and connection',403)
        result=json.loads(original.get('result') or '{}')
        if not result.get('ok') or result.get('data',{}).get('state')!='ready':
            raise DevError('WORKTREE_NOT_READY','Observe the original worktree creation receipt before using it',409,
                           operation_id=identifier)
        context=json.loads(original['route_context'])
        source=source_views(self.store,project)
        view=source['views'].get(original['device_id'])
        if not view or target_fingerprint(view)!=context['view_hash']:
            raise DevError('WORKTREE_SOURCE_CHANGED','The worktree belongs to its original source view; it was not moved or replicated',409)
        return original['device_id']

    def select(self,project,tool,args,principal,*,family=None,pinned_device=None):
        principal=iam.live_principal(self.store,principal)
        if project['space_id']!=principal.space_id:
            raise DevError('PROJECT_NOT_FOUND','Project is not provided by this connection',404)
        family=family or family_for(tool,args)
        require_access(self.store,principal,'read',project_id=project['id'])
        worktree_device=self._worktree_device(project,args,principal)
        requested=args.get('execution_device_id') or None
        if worktree_device and requested and requested!=worktree_device:
            raise DevError('WORKTREE_DEVICE_FIXED','Node-local worktrees cannot be redirected to another device',409)
        selected=worktree_device or pinned_device or requested
        if family=='files' and selected:
            if requested and not worktree_device and not pinned_device:
                raise DevError('INVALID_DEVICE_SELECTION','File tools use the workspace files mapping')
            source=source_views(self.store,project)
            view=source['views'].get(selected)
            if not view:
                raise DevError('SOURCE_DEVICE_UNAVAILABLE','The original source view is no longer available',409)
        else:
            view=select_view(self.store,project,family,selected)
        require_target_consent(self.store,principal,project,view)
        space,local,_,_=settings(self.store,project)
        plan=plan_hooks(family,{'provider':'device'},space.hooks,local.hooks)
        if plan['provider']=='rch' and tool not in {'exec','routed_exec','routed_job_control','project_tests_list','get_execution_options'}:
            raise DevError('RCH_EXPLICIT_TEST_REQUIRED','Use exec action=test with direct argv or a configured test task',409)
        context={'family':family,'device_id':view['device_id'],'source_key':view['source_key'],
                 'subpath':view['source_path'],'view_hash':target_fingerprint(view),'plan_hash':fingerprint(plan)}
        return {**project,'device_id':view['device_id'],'device_name':view['device_name'],'device_enabled':1,
                'root':view['root'],'logical_root':view['logical_root'],'mode':view['mode'],'allow_tasks':view['allow_tasks'],
                '_route_context':context,'_execution_plan':plan,
                '_source_boundary':{'mount':view['mount'],'subpath':view['subpath'],'protocol':1}}

    def prepare_execution(self,name,args,project):
        if name!='exec' or args.get('target','agent')!='agent':
            return name,args
        values=dict(args)
        if values.get('action')=='test' and values.get('task'):
            space,local,_,_=settings(self.store,project)
            recipes={r.name:r for r in space.tests}
            recipes.update({r.name:r for r in local.tests})
            recipe=recipes.get(values['task'])
            if not recipe:
                raise DevError('TEST_COMMAND_NOT_FOUND','This project has no configured test with that name',404)
            if values.get('env') or values.get('cwd','.')!='.':
                raise DevError('TEST_COMMAND_OVERRIDE','A configured test uses its reviewed cwd and environment',400)
            values.update(argv=recipe.argv,command='',task='',cwd=recipe.cwd,
                          timeout_seconds=min(values.get('timeout_seconds',3600),recipe.timeout_seconds))
        values.pop('execution_device_id',None)  # Trusted selection is already pinned in the project envelope.
        return 'routed_exec',RoutedExecution.model_validate(values).model_dump()

    def hydrate(self,project,principal):
        require_access(self.store,principal,'read',project_id=project['id'])
        try:
            view=select_view(self.store,project)
            require_target_consent(self.store,principal,project,view)
            folder=self.store.one('SELECT mode,allow_tasks FROM space_folders WHERE id=? AND space_id=?',
                                  (project['folder_id'],principal.space_id))
            return {**project,'device_id':view['device_id'],'device_name':view['device_name'],'root':view['root'],
                    'logical_root':view['logical_root'],'device_enabled':1,'mode':view['mode'],
                    'allow_tasks':bool(folder['allow_tasks']),'source_bound':True}
        except DevError as exc:
            return {**project,'device_id':'','device_name':'','root':'','logical_root':'','device_enabled':0,
                    'mode':'read','allow_tasks':False,'source_bound':False,'source_error':exc.code}

    def before_admission(self,project,name,args,caller):
        if name=='session' and project.get('_session_context') and not project['_session_context'].get('opening'):
            self.runtime.execution_sessions.validate(caller,project)
            return project,name,args
        if args.get('target','agent')!='agent':
            selected=self.runtime.vps.execution_project(args,caller)
            if project.get('_session_context'):
                selected['_session_context']=project['_session_context']
            return selected,name,args
        if not project.get('id'):
            return project,name,args
        current=iam.project_in_space(self.store,caller,project['id'])
        pinned=project.get('_route_context',{}).get('device_id')
        if name=='routed_job_control':
            original=self.runtime.operation_row(args['original_operation_id'],caller)
            pinned=original['device_id']
        selected=self.select(current,name,args,caller,pinned_device=pinned)
        if project.get('_route_context') and project['_route_context']!=selected['_route_context']:
            raise DevError('EXECUTION_PLAN_CHANGED','The source or execution settings changed before admission',409)
        if name=='routed_job_control':
            self.authorize_job_control(selected,args,caller)
        if project.get('_session_context'):
            selected['_session_context']=project['_session_context']
        name,args=self.prepare_execution(name,args,selected)
        return selected,name,args

    def revalidate(self,op,request,caller):
        if request['project'].get('_vps_context'):
            return self.runtime.vps.permission_error(request,op.get('project_id'),caller)
        if not request['project'].get('_route_context'):
            return None
        project=self.store.one('SELECT * FROM projects WHERE id=? AND space_id=?',(op['project_id'],caller.space_id))
        if not project:
            return 'The original project no longer exists in this workspace'
        try:
            current=self.select(project,op['tool'],request['args'],caller,pinned_device=op['device_id'])
        except DevError as exc:
            return exc.message
        if (current['_route_context']!=request['project']['_route_context'] or current['root']!=request['project']['root']
                or current['alias']!=request['project']['alias']):
            return 'The original source or execution plan changed; the operation was not redirected'
        return None

    def source_blocker(self,op):
        if not op.get('route_context') or op.get('attempts') or op['tool']=='routed_job_control':
            return None
        if op['tool']=='session' and json.loads(op.get('args_summary') or '{}').get('operation') in {'read','status','close','signal'}:
            # Authenticated controls must reach their original process even while
            # its command holds a source claim. They never select a new target.
            return None
        context=json.loads(op['route_context'])
        if not context.get('source_key'):
            return None
        # Shared bytes can be bound by multiple workspaces. Coordinate by source
        # identity, not workspace ownership; never disclose the other private receipt.
        rows=self.store.all('''SELECT id,tool,state,route_context,args_summary FROM operations
            WHERE id<>? AND source_lock_released=0 AND route_context IS NOT NULL
            AND (created<? OR (created=? AND id<?))
            AND state IN ('queued','running','reconnecting','cancelling','needs_review') ORDER BY created,id''',
            (op['id'],op['created'],op['created'],op['id']))
        from shared.contracts import MUTATING,PROCESS_TOOLS
        for row in rows:
            if row['tool']=='routed_job_control' or (row['tool']=='session' and json.loads(row.get('args_summary') or '{}').get('operation') in {'read','status','close','signal'}):
                continue
            other=json.loads(row['route_context'])
            if context['source_key']!=other.get('source_key'):
                continue
            path_type=PureWindowsPath if ':' in context['subpath'] or context['subpath'].startswith('\\\\') else PurePosixPath
            a,b=path_type(context['subpath']),path_type(other['subpath'])
            if (a==b or a in b.parents or b in a.parents) and (row['state']=='needs_review'
                    or op['tool'] in MUTATING|PROCESS_TOOLS or row['tool'] in MUTATING|PROCESS_TOOLS):
                return row['id']
        return None

    @staticmethod
    def require_protocol(connection):
        if getattr(connection,'workspace_protocol',0)!=1 or getattr(connection,'project_routing_protocol',0)!=1:
            raise DevError('WORKSPACE_RUNNER_REQUIRED','Update Hub and Runner together: workspace/source protocol 1 is required',409)

    def require_receipt(self,operation,principal):
        if operation.get('project_id'):
            require_access(self.store,principal,'read',project_id=operation['project_id'])
        context=json.loads(operation.get('route_context') or '{}')
        if context.get('vps_id'):
            self.runtime.vps.require(context['vps_id'],principal,'execute')
        # Original receipts are connection-private and reflect historical execution;
        # a changed default is not a reason to resolve or run another operation.

    def authorize_job_control(self,project,args,principal):
        original=self.runtime.operation_row(args['original_operation_id'],principal)
        if (original['tool']!='routed_exec' or original['project_id']!=project['id'] or original['device_id']!=project['device_id']
                or json.loads(original.get('route_context') or '{}')!=project['_route_context']
                or original['grant_id']!=principal.grant_id or original['owner_user_id']!=principal.user_id):
            raise DevError('RCH_ORIGINAL_SCOPE','RCH recovery must use the original connection, source, plan and launcher',403)

    def complete_control(self,operation,data):
        if operation['tool']!='routed_job_control' or not data.get('command_ok') or (data.get('rch') or {}).get('terminal_acknowledged') is not True:
            return
        original=self.store.one('SELECT * FROM operations WHERE id=?',(data.get('original_operation_id'),))
        if not original or any(original[k]!=operation[k] for k in ('project_id','device_id','grant_id','owner_user_id','route_context')):
            return
        self.store.execute('UPDATE operations SET source_lock_released=1 WHERE id=?',(original['id'],))
        self.runtime.wake_delivery()

    def tests(self,project,principal):
        require_access(self.store,principal,'read',project_id=project['id'])
        space,local,_,_=settings(self.store,project)
        recipes={r.name:r.model_dump() for r in space.tests}
        recipes.update({r.name:r.model_dump() for r in local.tests})
        plan=plan_hooks('test',{'provider':'device'},space.hooks,local.hooks)
        return {'tests':list(recipes.values()),'provider':plan['provider'],
                'invocation':{'tool':'exec','action':'test','task':'<name>'}}

    def options(self,project,principal,action='shell'):
        return execution_options(self.store,self.runtime,principal,project,action)
