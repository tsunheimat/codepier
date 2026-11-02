"""Whole VPS resources bound into workspaces; SSH uses Runner-local references.

Project associations are display categories, not execution projects, filesystem
roots, credential selectors or extra grants. Both entries reference one binding.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import replace

from fastapi import APIRouter, Query, Request
from pydantic import Field

from hub import iam
from hub.db_worker import database_endpoint
from hub.permissions import principal_policy, vps_actions, require_access
from hub.workspace_mutations import label_key, mutate
from hub.workspace_resources import owned_resource, checked_version
from hub.workspace_sources import inventory_device, require_vps_consent, vps_fingerprint
from shared.workspace_contracts import WorkspaceModel, VPSBindingInput
from shared.util import DevError


def name_key(value):
    return label_key(value)[1]


class VPSMetadataInput(WorkspaceModel):
    name: str=Field(min_length=1,max_length=253)
    description: str=Field(default='',max_length=2000)
    enabled: bool=True
    expected_version: int=Field(default=0,ge=0)
    idempotency_key: str=Field(min_length=8,max_length=128)


class VPSBindingSave(VPSBindingInput):
    expected_version: int=Field(default=0,ge=0)
    idempotency_key: str=Field(min_length=8,max_length=128)


class VPSService:
    def __init__(self,runtime):
        self.runtime,self.store=runtime,runtime.store

    def row(self,identifier,space_id):
        return self.store.one('''SELECT v.*,r.runner_device_id,r.host_alias,r.connection_ref,r.target_fingerprint,
            r.target_version,r.metadata,r.source_status,r.source_error,r.observed_at,r.enabled AS resource_enabled,d.name AS runner_name
            FROM space_vps v JOIN vps_connections r ON r.id=v.resource_id
            JOIN devices d ON d.id=r.runner_device_id WHERE v.id=? AND v.space_id=?''',(identifier,space_id))

    def actions(self,identifier,principal):
        principal=iam.live_principal(self.store,principal)
        if not self.row(identifier,principal.space_id):
            return set()
        return vps_actions(principal_policy(self.store,principal),identifier)

    def require(self,identifier,principal,action='read'):
        principal=iam.live_principal(self.store,principal)
        row=self.row(identifier,principal.space_id)
        if not row or action not in vps_actions(principal_policy(self.store,principal),identifier):
            raise DevError('VPS_NOT_FOUND','This workspace connection does not provide the requested VPS action',404)
        if action=='execute':
            if not row['enabled'] or not row['resource_enabled']:
                raise DevError('VPS_DISABLED','The workspace VPS resource is disabled',403)
            inventory_device(self.store,row['runner_device_id'])
            if row['source_status']!='configured':
                raise DevError('VPS_SOURCE_UNAVAILABLE','Resolve this VPS status in the owning Device settings',409,source_status=row['source_status'],source_error=row['source_error'])
            require_vps_consent(self.store,principal,row)
        return principal

    def can_execute(self,row,principal):
        try:
            self.require(row['id'],principal,'execute')
            return True
        except DevError:
            return False

    def public(self,row,principal,*,management=False):
        result={k:row[k] for k in ('id','name','host_alias','description','display_mode','version')}
        result['metadata']=json.loads(row['metadata'])
        result.update(target='vps:'+row['id'],enabled=bool(row['enabled'] and row['resource_enabled']),
                      shell_access=bool(self.actions(row['id'],principal)),can_execute=self.can_execute(row,principal),
                      online=self.runtime.online(row['runner_device_id']))
        projects=[]
        for project in self.store.all('SELECT p.id,p.alias FROM vps_projects vp JOIN projects p ON p.id=vp.project_id WHERE vp.vps_id=? AND vp.space_id=? ORDER BY p.alias_key',
                                      (row['id'],principal.space_id)):
            try:
                require_access(self.store,principal,'read',project_id=project['id'])
                projects.append(project)
            except DevError:
                if management:
                    projects.append(project)
        result['projects']=projects
        upstream=self.inventory_public(self.store.one('SELECT * FROM vps_connections WHERE id=?',(row['resource_id'],)))
        result['metadata']=upstream['metadata']
        result.update(status=upstream['status'],source_status=upstream['source_status'],source_error=upstream['source_error'],observed_at=upstream['observed_at'])
        if management:
            result.update(resource_id=row['resource_id'],runner_device_id=row['runner_device_id'],
                          runner_name=row['runner_name'])
        return result

    def get(self,identifier,principal):
        principal=self.require(identifier,principal)
        return self.public(self.row(identifier,principal.space_id),principal)

    def list(self,args,principal):
        principal=iam.live_principal(self.store,principal)
        chosen=None
        if args.get('project'):
            chosen=self.runtime.project(args['project'],principal)['id']
        parameters=[principal.space_id]
        sql='SELECT id FROM space_vps WHERE space_id=? AND enabled=1'
        if chosen:
            sql+=" AND display_mode IN ('project_only','both') AND EXISTS(SELECT 1 FROM vps_projects vp WHERE vp.vps_id=space_vps.id AND vp.project_id=? AND vp.space_id=space_vps.space_id)"
            parameters.append(chosen)
        else:
            sql+=" AND display_mode IN ('top_level','both')"
        sql+=' ORDER BY name_key,id'
        query=args.get('query','').strip().casefold()
        values=[]
        for item in self.store.all(sql,parameters):
            row=self.row(item['id'],principal.space_id)
            if 'read' not in self.actions(row['id'],principal):
                continue
            if query and not any(query in str(row[k]).casefold() for k in ('name','host_alias','description')):
                continue
            values.append(row)
        offset,limit=args.get('offset',0),args.get('limit',50)
        return {'vps':[self.public(r,principal) for r in values[offset:offset+limit]],'total':len(values),
                'next_offset':offset+limit if offset+limit<len(values) else None}

    def inventory_public(self,row):
        result={key:row[key] for key in ('id','name','runner_device_id','host_alias','description','enabled','version','created','updated',
                                          'source_status','source_error','observed_at')}
        device=self.store.one('SELECT name,enabled FROM devices WHERE id=?',(row['runner_device_id'],))
        result.update(runner_name=device['name'] if device else '',online=self.runtime.online(row['runner_device_id']),
                      metadata=json.loads(row['metadata']))
        result['status']=row['source_status']
        if not row['enabled'] or not device or not device['enabled']:
            result['status']='disabled'
        elif not result['online']:
            result['status']='offline'
        result['stale']=not result['online']
        # Observations belong to inventory. Merely opening a Space does not
        # expire a configured resource or dispatch a fresh SSH scan.
        from hub.runner_settings import cache_key
        probe=self.store.one('SELECT value FROM meta WHERE key=?',(cache_key(row['runner_device_id'],'probe:'+row['host_alias']),))
        observed=json.loads(probe['value']) if probe else {}
        if observed.get('target_fingerprint')==row['target_fingerprint']:
            result['last_probe']={key:observed.get(key) for key in ('tested','ready','error','observed_at','metadata')}
            if isinstance(observed.get('metadata'),dict):result['metadata']=observed['metadata']
            if result['status']=='configured' and observed.get('tested'):
                result['status']='ready' if observed.get('ready') else 'connection_failed'
                result['source_error']=observed.get('error') or ''
        return result

    def save(self,body,principal,identifier):
        def authorize_replay(current,receipt):
            owned_resource(self.store,current,'vps_connections',receipt['id'])
        def write(current):
            old=owned_resource(self.store,current,'vps_connections',identifier)
            checked_version(old,body.expected_version)
            name,key=label_key(body.name)
            self.store.db.execute('UPDATE vps_connections SET name=?,name_key=?,description=?,enabled=?,version=version+1,updated=? WHERE id=?',
                (name,key,body.description,int(body.enabled),time.time(),identifier))
            self.runtime.workspace_resources.changed(current,identifier)
            return self.inventory_public(self.store.one('SELECT * FROM vps_connections WHERE id=?',(identifier,)))
        return mutate(self.store,principal,'vps.metadata:'+identifier,body.idempotency_key,body.model_dump(),write,
                      authorize_replay=authorize_replay)

    def bind(self,body,principal,identifier=None):
        def inventory_relationship(current):
            owner=replace(current,space_id=None)
            resource=owned_resource(self.store,owner,'vps_connections',body.resource_id)
            # Resolve the dependency from the resource, never from Space input.
            # A receipt must not preserve authority after Runner ownership moves.
            owned_resource(self.store,owner,'devices',resource['runner_device_id'])
            return resource
        def authorize_replay(current,receipt):
            iam.require_space_capability(self.store,current,'resources')
            inventory_relationship(current)
            if not self.row(receipt['id'],current.space_id):
                raise DevError('VPS_NOT_FOUND','Workspace VPS binding not found',404)
            for project_id in body.projects:
                iam.project_in_space(self.store,current,project_id)
        def write(current):
            iam.require_space_capability(self.store,current,'resources')
            resource=inventory_relationship(current)
            # Space binds the persisted upstream abstraction, even while offline.
            # It does not probe, refresh SSH or fabricate a replacement target.
            old=self.row(identifier,current.space_id) if identifier else None
            if identifier and not old:
                raise DevError('VPS_NOT_FOUND','Workspace VPS binding not found',404)
            checked_version(old,body.expected_version)
            if old and old['resource_id']!=body.resource_id:
                raise DevError('VPS_IDENTITY_IMMUTABLE','Create a new binding to use another inventory VPS',409)
            for project_id in body.projects:
                iam.project_in_space(self.store,current,project_id)
            selected=identifier or uuid.uuid4().hex
            name,key=label_key(body.name)
            values=(name,key,body.description,body.display_mode,int(body.enabled))
            try:
                if old:
                    self.store.db.execute('UPDATE space_vps SET name=?,name_key=?,description=?,display_mode=?,enabled=?,version=version+1 WHERE id=?',(*values,selected))
                else:
                    self.store.db.execute('INSERT INTO space_vps(name,name_key,description,display_mode,enabled,id,space_id,resource_id) VALUES(?,?,?,?,?,?,?,?)',(*values,selected,current.space_id,body.resource_id))
                existing={r['project_id']:r['binding_id'] for r in self.store.all('SELECT project_id,binding_id FROM vps_projects WHERE vps_id=?',(selected,))}
                self.store.db.execute('DELETE FROM vps_projects WHERE vps_id=?',(selected,))
                for project_id in body.projects:
                    self.store.db.execute('INSERT INTO vps_projects VALUES(?,?,?,?)',(current.space_id,selected,project_id,existing.get(project_id) or uuid.uuid4().hex))
            except sqlite3.IntegrityError as exc:
                raise DevError('VPS_BINDING_CONFLICT','The VPS is already bound, its name exists, or a selected resource is outside the workspace',409) from exc
            self.runtime.workspace_resources.changed(current,selected)
            return self.public(self.row(selected,current.space_id),current,management=True)
        return mutate(self.store,principal,'vps.bind:'+(identifier or 'new'),body.idempotency_key,body.model_dump(),write,
                      authorize_replay=authorize_replay)

    def execution_project(self,args,principal):
        target=args.get('target','')
        if not target.startswith('vps:') or args.get('execution_device_id'):
            raise DevError('VPS_TARGET_REQUIRED','Choose the exact saved VPS target; its Runner is fixed by the saved resource')
        identifier=target[4:]
        principal=self.require(identifier,principal,'execute')
        row=self.row(identifier,principal.space_id)
        project=None
        if args.get('project'):
            # Optional classification context requires read, not execution on a
            # completely unrelated local project directory.
            project=self.runtime.project(args['project'],principal)
            if not self.store.one('SELECT project_id FROM vps_projects WHERE vps_id=? AND project_id=? AND space_id=?',
                                  (identifier,project['id'],principal.space_id)):
                raise DevError('VPS_PROJECT_CONTEXT_INVALID','The optional project does not contain this VPS entry',403)
        context={'id':identifier,'resource_id':row['resource_id'],'runner_device_id':row['runner_device_id'],
                 'connection_ref':row['connection_ref'],'host_alias':row['host_alias'],
                 'target_fingerprint':row['target_fingerprint'],'target_version':row['target_version'],
                 'fingerprint':vps_fingerprint(row),'space_id':principal.space_id}
        return {'id':project['id'] if project else None,'alias':project['alias'] if project else 'vps:'+identifier,
                'space_id':principal.space_id,'root':'','mode':'write','allow_tasks':True,
                'device_id':row['runner_device_id'],'device_name':row['runner_name'],'device_enabled':1,
                '_vps_context':context,'_route_context':{'family':'ssh','vps_id':identifier,'device_id':row['runner_device_id'],
                    'source_key':'vps:'+row['resource_id'],'subpath':'.','view_hash':vps_fingerprint(row)}}

    def reference(self,args,project,principal):
        resolved=self.execution_project(args,principal)
        if resolved['_vps_context']!=project.get('_vps_context'):
            raise DevError('VPS_CONFIGURATION_CHANGED','The VPS connection changed before admission',409)
        return resolved['_vps_context']

    def _authorized_row(self,request,project_id):
        ref=request.get('vps_ref') or request['project'].get('_vps_context') or {}
        row=self.row(ref.get('id'),ref.get('space_id'))
        if row and self.runtime.online(row['runner_device_id']):
            from shared.tool_protocol import require_compatible
            require_compatible(getattr(self.runtime.connections.get(row['runner_device_id']), 'tool_protocol', None), 'runner_settings')
        if not row or not row['enabled'] or not row['resource_enabled'] or row['source_status']!='configured' or vps_fingerprint(row)!=ref.get('fingerprint'):
            raise DevError('VPS_CONFIGURATION_CHANGED','The original VPS connection changed; no replacement connection was selected',409)
        inventory_device(self.store,row['runner_device_id'])
        if project_id and not self.store.one('SELECT project_id FROM vps_projects WHERE vps_id=? AND project_id=? AND space_id=?',
                                             (row['id'],project_id,ref['space_id'])):
            raise DevError('VPS_CONTEXT_CHANGED','The optional project association was removed',409)
        return row

    def permission_error(self,request,project_id,principal):
        try:
            row=self._authorized_row(request,project_id)
            self.require(row['id'],principal,'execute')
            if project_id:
                require_access(self.store,principal,'read',project_id=project_id)
        except DevError as exc:
            return exc.message
        return None

    def transport(self,request,project_id):
        row=self._authorized_row(request,project_id)
        # No password, arbitrary Host argument or private key crosses this boundary.
        # The Runner resolves this reviewed connection reference in its own config.
        return {**request,'project':{**request['project'],'_ssh_target':{
            'protocol':2,'vps_id':row['id'],'resource_id':row['resource_id'],
            'connection_ref':row['connection_ref'],'host_alias':row['host_alias'],
            'target_fingerprint':row['target_fingerprint']}}}


def make_vps_router(auth,runtime):
    router,store,service=APIRouter(),runtime.store,runtime.vps

    @router.get('/api/vps')
    @database_endpoint(store)
    def inventory(request: Request):
        return {'vps':[service.inventory_public(row) for row in runtime.workspace_resources.inventory(auth.panel(request),'vps_connections')]}

    @router.put('/api/vps/{identifier}')
    @database_endpoint(store)
    def update_vps(identifier: str,request: Request,body: VPSMetadataInput):
        return service.save(body,auth.panel(request,True),identifier)

    @router.delete('/api/vps/{identifier}')
    @database_endpoint(store)
    def remove_vps(identifier: str,request: Request,expected_version: int=Query(ge=1)):
        with store.transaction():
            principal=auth.panel(request,True)
            resource=owned_resource(store,principal,'vps_connections',identifier)
            checked_version(resource,expected_version)
            if resource['source_status']!='withdrawn':
                raise DevError('VPS_SOURCE_MANAGED','Revoke the SSH Alias in the owning Device before removing this derived resource',409)
            if store.one('SELECT id FROM space_vps WHERE resource_id=? LIMIT 1',(identifier,)):
                raise DevError('VPS_IN_USE','Remove this VPS from workspace bindings first',409)
            store.db.execute('DELETE FROM vps_connections WHERE id=?',(identifier,))
            iam.audit(store,principal,'vps.inventory_removed',identifier,detail={'remote_server_deleted':False})
        return {'id':identifier,'remote_server_deleted':False}

    @router.get('/api/spaces/{space_id}/vps')
    @database_endpoint(store)
    def workspace_vps(request: Request):
        principal=auth.panel(request)
        try:
            iam.require_space_capability(store,principal,'resources')
            management=True
        except DevError:
            management=False
        return {'vps':[service.public(service.row(r['id'],principal.space_id),principal,management=management)
                       for r in store.all('SELECT id FROM space_vps WHERE space_id=? ORDER BY name_key,id',(principal.space_id,))
                       if management or 'read' in service.actions(r['id'],principal)]}

    @router.get('/api/projects/{project_id}/vps')
    @database_endpoint(store)
    def project_vps(project_id: str,request: Request):
        return service.list({'project':project_id},auth.panel(request))

    @router.post('/api/spaces/{space_id}/vps',status_code=201)
    @database_endpoint(store)
    def bind_vps(request: Request,body: VPSBindingSave):
        return service.bind(body,auth.panel(request,True))

    @router.put('/api/spaces/{space_id}/vps/{identifier}')
    @database_endpoint(store)
    def update_binding(identifier: str,request: Request,body: VPSBindingSave):
        return service.bind(body,auth.panel(request,True),identifier)

    @router.delete('/api/spaces/{space_id}/vps/{identifier}')
    @database_endpoint(store)
    def remove_binding(identifier: str,request: Request,expected_version: int=Query(ge=1)):
        with store.transaction():
            principal=iam.require_space_capability(store,auth.panel(request,True),'resources')
            row=service.row(identifier,principal.space_id)
            if not row:
                raise DevError('VPS_NOT_FOUND','Workspace VPS binding not found',404)
            checked_version(row,expected_version)
            if store.one('SELECT id FROM execution_sessions WHERE vps_id=? LIMIT 1',(identifier,)):
                raise DevError('VPS_HAS_SESSIONS','This VPS has retained Session records; disable the binding instead',409)
            store.db.execute('DELETE FROM vps_projects WHERE vps_id=? AND space_id=?',(identifier,principal.space_id))
            store.db.execute('DELETE FROM space_vps WHERE id=?',(identifier,))
            iam.audit(store,principal,'vps.unbound',identifier,detail={'remote_server_deleted':False,'remote_processes_stopped':False})
            runtime.workspace_resources.changed(principal,identifier)
        return {'id':identifier,'remote_server_deleted':False,'remote_processes_stopped':False}
    return router
