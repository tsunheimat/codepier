"""Connection-private persistent execution identities with immutable targets.

A Session open is a durable operation; later commands/input have their own durable
operation IDs. Hub restart never creates a replacement Shell to fake restoration.
"""
from __future__ import annotations

import json
import time

from hub import iam
from hub.permissions import require_access
from hub.workspace_sources import fingerprint
from shared.util import DevError


def envelope(project):
    keys=('id','alias','root','mode','allow_tasks','device_id','space_id','_vps_context','_route_context',
          '_execution_plan','_source_boundary','_integration_owner','_coding_owner','_coding_scopes')
    return {k:project[k] for k in keys if k in project}


class ExecutionSessions:
    def __init__(self,runtime):
        self.runtime,self.store=runtime,runtime.store

    def row(self,identifier,principal):
        principal=iam.live_principal(self.store,principal)
        row=self.store.one('SELECT * FROM execution_sessions WHERE id=? AND space_id=?',(identifier,principal.space_id))
        if not row or row['grant_id']!=principal.grant_id or row['owner_user_id']!=principal.user_id:
            raise DevError('SESSION_NOT_FOUND','Session is not owned by this connection',404)
        return row

    def prepare(self,args,principal):
        principal=iam.live_principal(self.store,principal)
        if args['operation']=='open':
            if args['target']!='agent':
                project=self.runtime.vps.execution_project(args,principal)
            else:
                original=self.runtime.project(args['project'],principal)
                require_access(self.store,principal,'execute',project_id=original['id'])
                project=self.runtime.routing.select(original,'session',args,principal)
                if project['_execution_plan']['provider']!='device':
                    raise DevError('SESSION_PROVIDER_UNSUPPORTED','Interactive Sessions use a direct local or saved SSH target, not an RCH job')
            if not self.runtime.online(project['device_id']):
                raise DevError('SESSION_DEVICE_OFFLINE','Open requires the selected Runner to be online; no fallback was selected',409)
            return {**project,'_session_context':{'opening':True,'mode':args['mode']}}
        row=self.row(args['session_id'],principal)
        context=json.loads(row['context'])
        if row['state'] in {'failed','lost','closed','expired'} and args['operation'] not in {'read','status','close'}:
            raise DevError('SESSION_NOT_ACTIVE','The original Session is no longer active; its state was not recreated',409,session_id=row['id'])
        if row['mode']=='pty' and args['operation']=='exec' or row['mode']=='persistent_shell' and args['operation'] in {'write','resize'}:
            raise DevError('SESSION_MODE_MISMATCH','Persistent shells use exec; PTYs use raw terminal read/write/resize')
        project={**context['project'],'_session_context':{'id':row['id'],'binding_hash':fingerprint(context),'mode':row['mode']}}
        self.validate(principal,project)
        return project

    def validate(self,principal,project):
        context=project.get('_session_context') or {}
        if context.get('opening'):
            if project.get('_vps_context'):
                self.runtime.vps.require(project['_vps_context']['id'],principal,'execute')
            else:
                require_access(self.store,principal,'execute',project_id=project['id'])
            return
        row=self.row(context.get('id'),principal)
        original=json.loads(row['context'])
        if context.get('binding_hash')!=fingerprint(original):
            raise DevError('SESSION_BINDING_CHANGED','The original Session binding changed; it cannot be retargeted',409)
        if envelope(project)!=original['project']:
            raise DevError('SESSION_BINDING_CHANGED','Session commands must use their original target envelope',409)
        if project.get('_vps_context'):
            denied=self.runtime.vps.permission_error({'project':project},row['project_id'],principal)
        else:
            require_access(self.store,principal,'execute',project_id=row['project_id'])
            operation={'project_id':row['project_id'],'device_id':row['device_id'],'tool':'session'}
            denied=self.runtime.routing.revalidate(operation,{'project':project,'args':original['open_args']},principal)
        if denied:
            raise DevError('SESSION_ACCESS_CHANGED',denied,403,session_id=row['id'])

    def admitted(self,identifier,args,project,principal):
        self.store.require_transaction()
        if args['operation']!='open':
            self.validate(principal,project)
            return
        context={'project':envelope(project),'open_args':args}
        now=time.time()
        self.store.db.execute('''INSERT INTO execution_sessions(id,space_id,owner_user_id,grant_id,project_id,vps_id,device_id,mode,context,state,created,updated)
            VALUES(?,?,?,?,?,?,?,?,?,'opening',?,?)''',
            (identifier,principal.space_id,principal.user_id,principal.grant_id,project.get('id'),
             project.get('_vps_context',{}).get('id'),project['device_id'],args['mode'],json.dumps(context,sort_keys=True),now,now))

    def completed(self,operation,result):
        if operation['tool']!='session':
            return
        summary=json.loads(operation['args_summary'])
        identifier=operation['id'] if summary.get('operation')=='open' else summary.get('session_id')
        row=self.store.one('SELECT * FROM execution_sessions WHERE id=?',(identifier,))
        if not row or row['grant_id']!=operation['grant_id'] or row['owner_user_id']!=operation['owner_user_id']:
            return
        data=result.get('data') or {}
        state=data.get('state')
        if state not in {'active','closed','expired','lost','failed'}:
            if summary.get('operation')=='open' and not result.get('ok'):
                state='failed'
            elif (result.get('error') or {}).get('code')=='SESSION_LOST':
                state='lost'
            else:
                return
        if row['state'] in {'failed','lost','closed','expired'}:
            return  # A delayed active/status response cannot resurrect a closed Session.
        self.store.execute('UPDATE execution_sessions SET state=?,updated=? WHERE id=?',(state,time.time(),identifier))
