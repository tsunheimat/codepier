"""Inventory resources and atomic, Runner-validated workspace source mutations."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import replace

from hub import iam
from hub.permissions import require_access
from hub.workspace_mutations import label_key, mutate
from hub.workspace_sources import (fingerprint, source_views, resolve_shell, shell_settings,
    validate_project_shell, configured_source, inventory_device, pool_defaults)
from shared.workspace_contracts import (StoragePoolInput, StorageMappingInput, FolderInput,
    SourceInput, ProjectInput, absolute_path, mapped_path)
from shared.util import DevError


def checked_version(row, expected):
    if type(expected) is not int or (row['version'] if row else 0) != expected:
        raise DevError('VERSION_CHANGED', 'Configuration changed; reload it before saving', 409)


def owned_resource(store, principal, table, identifier):
    if table not in {'devices', 'folders', 'storage_pools', 'vps_connections', 'gateway_connectors'}:
        raise ValueError('Unknown inventory resource type')
    principal = iam.live_principal(store, principal)
    row = store.one('SELECT * FROM '+table+' WHERE id=?', (identifier,))
    if not row or principal.grant_id or not (principal.instance_admin or row['owner_user_id']==principal.user_id):
        raise DevError('INVENTORY_RESOURCE_NOT_FOUND', 'Inventory resource is not owned by this account', 404)
    return row


def public_pool(row):
    return {**row,'shell_allowed_device_ids':json.loads(row['shell_allowed_device_ids'])}


def public_source(row):
    return {**row,'files':json.loads(row['files']),'shell':json.loads(row['shell'])}


class WorkspaceResources:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def changed(self, principal, identifier):
        self.store.after_commit(lambda: self.runtime.publish('project', {'id': identifier, 'space_id': principal.space_id}))
        self.runtime.wake_delivery()

    def inventory(self, principal, table):
        if table not in {'folders','storage_pools', 'vps_connections'}:
            raise ValueError('Unknown inventory')
        principal = iam.live_principal(self.store, principal)
        if principal.grant_id:
            raise DevError('PANEL_REQUIRED', 'Inventory requires a panel account', 403)
        rows=self.store.all('SELECT * FROM '+table+(' ORDER BY name,id' if principal.instance_admin else ' WHERE owner_user_id=? ORDER BY name,id'),
                              () if principal.instance_admin else (principal.user_id,))
        return [public_pool(r) for r in rows] if table=='storage_pools' else rows

    def _pool_base(self, fields, identifier):
        base=absolute_path(fields.abstract_base)
        for other in self.store.all('SELECT id,abstract_base FROM storage_pools WHERE id<>?',(identifier or '',)):
            candidate=absolute_path(other['abstract_base'])
            if base==candidate or base in candidate.parents or candidate in base.parents:
                raise DevError('ABSTRACT_PATH_CONFLICT','Abstract pool bases cannot overlap',409)

    def _pool_state(self, pool_id):
        pool=self.store.one('SELECT * FROM storage_pools WHERE id=?',(pool_id,))
        mappings=self.store.all('SELECT * FROM storage_mappings WHERE pool_id=? AND enabled=1',(pool_id,))
        devices={}
        for mapping in mappings:
            try:
                devices[mapping['device_id']]=inventory_device(self.store,mapping['device_id'])
            except DevError:
                pass
        pool_defaults(pool,mappings,devices)

    def save_pool(self, principal, body, identifier):
        fields=StoragePoolInput.model_validate(body.model_dump(include=set(StoragePoolInput.model_fields)))
        def save(current):
            old=owned_resource(self.store,current,'storage_pools',identifier)
            checked_version(old,body.expected_version)
            self._pool_base(fields,identifier)
            name,_=label_key(fields.name)
            self.store.db.execute('''UPDATE storage_pools SET name=?,abstract_base=?,enabled=?,default_fs_device_id=?,
                default_shell_device_id=?,shell_allowed_device_ids=?,version=version+1,updated=? WHERE id=?''',
                (name,fields.abstract_base,int(fields.enabled),fields.default_fs_device_id,fields.default_shell_device_id,
                 json.dumps(fields.shell_allowed_device_ids),time.time(),identifier))
            self._pool_state(identifier)
            if fields.enabled:
                self.validate_affected(pool_id=identifier)
            self.changed(current,identifier)
            return public_pool(self.store.one('SELECT * FROM storage_pools WHERE id=?',(identifier,)))
        return mutate(self.store,principal,'storage-pool.save:'+identifier,body.idempotency_key,body.model_dump(),save)

    def bind_device(self, principal, device_id, body):
        def save(current):
            iam.require_space_capability(self.store,current,'resources')
            owned_resource(self.store,replace(current,space_id=None),'devices',device_id)
            row=self.store.one('SELECT * FROM space_devices WHERE space_id=? AND device_id=?',(current.space_id,device_id))
            checked_version(row,body.expected_version)
            # Independent allocation: source mappings and SSH launchers are not Device grants.
            self.store.db.execute('''INSERT INTO space_devices(space_id,device_id,enabled) VALUES(?,?,?)
                ON CONFLICT(space_id,device_id) DO UPDATE SET enabled=excluded.enabled,version=space_devices.version+1''',
                (current.space_id,device_id,int(body.enabled)))
            self.changed(current,device_id)
            return self.store.one('SELECT * FROM space_devices WHERE space_id=? AND device_id=?',(current.space_id,device_id))
        return mutate(self.store,principal,'device.bind:'+device_id,body.idempotency_key,body.model_dump(),save)

    def validate_affected(self, space_id=None, *, source_id=None, pool_id=None, folder_resource_id=None):
        sql='SELECT * FROM space_sources WHERE enabled=1'
        args=[]
        for key,value in [('space_id',space_id),('id',source_id),('pool_id',pool_id),('folder_resource_id',folder_resource_id)]:
            if value is not None:
                sql+=' AND '+key+'=?';args.append(value)
        conflicts=[]
        for binding in self.store.all(sql,args):
            # A child's override must never hide an invalid inherited source
            # default. Validate the source root independently before its users.
            projects=[{'id':'','source_id':binding['id'],'space_id':binding['space_id'],'subpath':'.'},
                      *self.store.all('SELECT * FROM projects WHERE source_id=?',(binding['id'],))]
            for project in projects:
                try:
                    source=source_views(self.store,project)
                    if source['files_device_id'] not in source['views']:
                        raise DevError('FILES_DEVICE_UNAVAILABLE','The source needs its files mapping',409)
                    if binding['allow_tasks'] and not source['shell_disabled'] and shell_settings(self.store,project).mode!='disabled':
                        resolve_shell(self.store,project,source=source)
                except DevError as exc:
                    conflicts.append({'source_id':binding['id'],'project_id':project['id'],'code':exc.code})
        if conflicts:
            raise DevError('WORKSPACE_CONFIGURATION_CONFLICT','This edit would invalidate source defaults or Projects; update their settings first',409,conflicts=conflicts)

    @staticmethod
    def _validation_view(device_id, path, mode='read', allow_tasks=False, *, subpath='.'):
        return {'device_id':device_id,'root':mapped_path(path,subpath),'alias':'source-validation','mode':mode,
                'allow_tasks':allow_tasks,'_source_boundary':{'mount':path,'subpath':subpath,'protocol':1}}

    def _prepare_validation(self, kind, principal, payload, identifier):
        principal=iam.live_principal(self.store,principal)
        if kind=='pool':
            if principal.grant_id or identifier:
                raise DevError('PANEL_REQUIRED','Pool creation requires an inventory account',403)
            checked_version(None,payload['expected_version'])
            fields=StoragePoolInput.model_validate({k:payload[k] for k in StoragePoolInput.model_fields})
            self._pool_base(fields,None)
            mappings=[StorageMappingInput.model_validate(m) for m in payload['mappings']]
            if not mappings or len({m.device_id for m in mappings})!=len(mappings) or any(not m.enabled for m in mappings):
                raise DevError('POOL_MAPPINGS_REQUIRED','Create a Pool with distinct enabled device mappings',409)
            devices=[owned_resource(self.store,principal,'devices',m.device_id) for m in mappings]
            for device in devices:
                inventory_device(self.store,device['id'])
            if not {fields.default_fs_device_id,fields.default_shell_device_id,*fields.shell_allowed_device_ids}<={m.device_id for m in mappings}:
                raise DevError('DEVICE_NOT_IN_SOURCE','Pool defaults must select the supplied mappings',403)
            return {'devices':[{k:d[k] for k in ('id','owner_user_id','enabled','info')} for d in devices],'views':[self._validation_view(m.device_id,m.path) for m in mappings]}
        if kind=='mapping':
            fields=StorageMappingInput.model_validate({k:payload[k] for k in StorageMappingInput.model_fields})
            pool=owned_resource(self.store,principal,'storage_pools',payload['pool_id'])
            device=owned_resource(self.store,principal,'devices',fields.device_id)
            inventory_device(self.store,device['id'])
            old=self.store.one('SELECT * FROM storage_mappings WHERE id=? AND pool_id=?',(identifier,pool['id'])) if identifier else None
            if identifier and not old:
                raise DevError('MAPPING_NOT_FOUND','Mapping not found',404)
            checked_version(old,payload['expected_version'])
            if old and old['device_id']!=fields.device_id:
                raise DevError('MAPPING_IDENTITY_IMMUTABLE','Create another mapping to change its device',409)
            return {'before':old,'pool':pool,'device':{k:device[k] for k in ('id','owner_user_id','enabled','info')},'views':[self._validation_view(fields.device_id,fields.path)]}
        if kind=='folder':
            fields=FolderInput.model_validate({k:payload[k] for k in FolderInput.model_fields})
            old=owned_resource(self.store,principal,'folders',identifier) if identifier else None
            checked_version(old,payload['expected_version'])
            device=owned_resource(self.store,principal,'devices',fields.device_id)
            inventory_device(self.store,device['id'])
            if old and old['device_id']!=fields.device_id:
                raise DevError('FOLDER_IDENTITY_IMMUTABLE','Create another Folder to change its device',409)
            return {'before':old,'device':{k:device[k] for k in ('id','owner_user_id','enabled','info')},'views':[self._validation_view(fields.device_id,fields.device_path,fields.mode,fields.allow_tasks)]}
        if kind=='source':
            iam.require_space_capability(self.store,principal,'resources')
            fields=SourceInput.model_validate({k:payload[k] for k in SourceInput.model_fields})
            old=self.store.one('SELECT * FROM space_sources WHERE id=? AND space_id=?',(identifier,principal.space_id)) if identifier else None
            if identifier and not old:
                raise DevError('SOURCE_NOT_FOUND','Workspace source not found',404)
            checked_version(old,payload['expected_version'])
            if old and any(old[k]!=getattr(fields,k) for k in ('source_kind','folder_resource_id','pool_id')):
                raise DevError('SOURCE_IDENTITY_IMMUTABLE','Create another source to change its inventory resource',409)
            table,resource_id=('folders',fields.folder_resource_id) if fields.source_kind=='folder' else ('storage_pools',fields.pool_id)
            owned_resource(self.store,replace(principal,space_id=None),table,resource_id)
            mappings=[]
            if fields.source_kind=='storage':
                rows=self.store.all('SELECT * FROM storage_mappings WHERE pool_id=? ORDER BY id',(fields.pool_id,))
                if fields.mapping_ids:
                    mappings=[m for m in rows if m['id'] in fields.mapping_ids]
                    if len(mappings)!=len(fields.mapping_ids):
                        raise DevError('DEVICE_NOT_IN_SOURCE','Select only mappings from this Pool',403)
                else:
                    mappings=[m for m in rows if m['enabled']]
            candidate={**fields.model_dump(),'id':identifier or '', 'space_id':principal.space_id,
                       'files':fields.files.model_dump_json(),'shell':fields.shell.model_dump_json()}
            resolved=configured_source(self.store,candidate,mappings)
            views=[self._validation_view(m['device_id'],m['path'],
                    'write' if fields.mode=='write' and m['writable'] else 'read',
                    bool(fields.allow_tasks and m['allow_tasks']),subpath=fields.subpath) for m in resolved['mappings']]
            # Source creation validates both defaults even before it has a Project.
            if fields.allow_tasks and not resolved['shell_disabled'] and resolved['default_device_id'] not in resolved['allowed_device_ids']:
                raise DevError('DEFAULT_DEVICE_UNAVAILABLE','Choose a shell default within this source',409)
            return {'before':old,'resource':resolved['resource'],'mappings':mappings,'views':views}
        if kind=='project':
            fields=ProjectInput.model_validate({k:payload[k] for k in ProjectInput.model_fields})
            if principal.grant_id:
                if identifier:
                    raise DevError('PROJECT_CONFIGURATION_DENIED','MCP cannot reconfigure an existing project',403)
                require_access(self.store,principal,'projects.create',source_id=fields.source_id)
            else:
                iam.require_space_capability(self.store,principal,'resources')
            old=self.store.one('SELECT * FROM projects WHERE id=? AND space_id=?',(identifier,principal.space_id)) if identifier else None
            if identifier and not old:
                raise DevError('PROJECT_NOT_FOUND','Project not found',404)
            checked_version(old,payload['expected_version'])
            candidate={'id':identifier or '', 'space_id':principal.space_id,'source_id':fields.source_id,'subpath':fields.subpath}
            source=source_views(self.store,candidate)
            if fields.alias.strip() in {'.','..'} or not re.fullmatch(r'[\w.-]{1,64}',fields.alias.strip()):
                raise DevError('INVALID_ALIAS','Use a project name, not a path or whitespace')
            occupied=self.store.one('SELECT id FROM projects WHERE space_id=? AND alias_key=?',(principal.space_id,label_key(fields.alias)[1]))
            if occupied and occupied['id']!=identifier:
                raise DevError('ALIAS_EXISTS','A project with this alias already exists in the workspace',409)
            views=[{'device_id':v['device_id'],'root':v['root'],'alias':fields.alias,'mode':v['mode'],'allow_tasks':v['allow_tasks'],
                    '_source_boundary':{'mount':v['mount'],'subpath':v['subpath'],'protocol':1}} for v in source['views'].values()]
            if source['source']['allow_tasks'] and not source['shell_disabled'] and shell_settings(self.store,candidate).mode!='disabled':
                resolve_shell(self.store,candidate,source=source)
            if not views:
                raise DevError('SOURCE_UNAVAILABLE','This workspace source has no enabled views',409)
            source['allowed_device_ids']=sorted(source['allowed_device_ids'])
            return {'before':old,'source':source,'views':views}
        raise DevError('INVALID_VALIDATION','Unsupported workspace validation intent')

    def _authorize_save_replay(self, kind, principal, payload, receipt, pool_id):
        if kind in {'pool','folder'}:
            owned_resource(self.store,principal,'storage_pools' if kind=='pool' else 'folders',receipt['id'])
        elif kind=='mapping':
            owned_resource(self.store,principal,'storage_pools',pool_id)
        elif kind=='project' and principal.grant_id:
            require_access(self.store,principal,'projects.create',source_id=payload['source_id'])
        else:
            iam.require_space_capability(self.store,principal,'resources')

    def _deactivate_source(self, principal, payload, identifier):
        """Withdraw a binding using only Hub state, preserving its configuration."""
        iam.require_space_capability(self.store,principal,'resources')
        old=self.store.one('SELECT * FROM space_sources WHERE id=? AND space_id=?',(identifier,principal.space_id))
        if not old:
            raise DevError('SOURCE_NOT_FOUND','Workspace source not found',404)
        checked_version(old,payload['expected_version'])
        stored={k:old[k] for k in SourceInput.model_fields if k in old}
        stored.update(enabled=bool(old['enabled']),allow_tasks=bool(old['allow_tasks']),
                      files=json.loads(old['files']),shell=json.loads(old['shell']),
                      mapping_ids=[m['mapping_id'] for m in self.store.all(
                          'SELECT mapping_id FROM space_source_mappings WHERE source_id=? AND space_id=?',
                          (identifier,principal.space_id))])
        current=SourceInput.model_validate(stored)
        requested=SourceInput.model_validate({k:payload[k] for k in SourceInput.model_fields})
        if (requested.model_dump(exclude={'enabled','mapping_ids'})!=current.model_dump(exclude={'enabled','mapping_ids'})
                or set(requested.mapping_ids)!=set(current.mapping_ids)):
            raise DevError('SOURCE_DEACTIVATION_ONLY','Disable the unchanged source first; configuration edits require a separate validated save',409)
        self.store.db.execute('UPDATE space_sources SET enabled=0,version=version+1 WHERE id=? AND space_id=?',
                              (identifier,principal.space_id))
        return self.source_public(self.store.one('SELECT * FROM space_sources WHERE id=?',(identifier,)))

    async def validated_save(self, kind, principal, body, identifier=None, *, pool_id=None, reauthenticate=None):
        payload=body.model_dump()
        if pool_id is not None:
            payload['pool_id']=pool_id
        operation=kind+'.save:'+(identifier or 'new')+(':'+pool_id if pool_id else '')
        request_fingerprint=fingerprint(payload)
        key='workspace-validation:'+fingerprint([principal.space_id,principal.user_id,principal.grant_id,operation,body.idempotency_key])
        def record(fresh, selected):
            self.store.db.execute('INSERT INTO workspace_requests VALUES(?,?,?,?,?,?,?)',
                (fresh.space_id or '',fresh.user_id,operation,body.idempotency_key,request_fingerprint,
                 json.dumps(selected,ensure_ascii=False),time.time()))
            iam.audit(self.store,fresh,operation,selected['id'],detail={'version':selected.get('version')})
            self.changed(fresh,selected['id'])
            return selected
        def prepare():
            with self.store.transaction():
                fresh=reauthenticate() if reauthenticate else iam.live_principal(self.store,principal)
                if (fresh.user_id,fresh.space_id,fresh.grant_id)!=(principal.user_id,principal.space_id,principal.grant_id):
                    raise DevError('IDENTITY_CHANGED','The request identity changed; no resource was saved',403)
                old=self.store.one('SELECT fingerprint,receipt FROM workspace_requests WHERE space_id=? AND user_id=? AND operation=? AND request_key=?',
                    (principal.space_id or '',principal.user_id,operation,body.idempotency_key))
                if old:
                    if old['fingerprint']!=request_fingerprint:
                        raise DevError('IDEMPOTENCY_CONFLICT','This request key identifies different resource arguments',409)
                    receipt=json.loads(old['receipt'])
                    self._authorize_save_replay(kind,fresh,payload,receipt,pool_id)
                    return None,{**receipt,'replayed':True}
                if kind=='source' and identifier and not payload['enabled']:
                    prior=self.store.one('SELECT value FROM meta WHERE key=?',(key,))
                    if prior and json.loads(prior['value'])['fingerprint']!=request_fingerprint:
                        raise DevError('VALIDATION_CHANGED','The original save request has different arguments; use a reviewed new save request',409)
                    return None,record(fresh,self._deactivate_source(fresh,payload,identifier))
                selected=self._prepare_validation(kind,fresh,payload,identifier)
                intent={'kind':kind,'payload':payload,'identifier':identifier,'user_id':fresh.user_id,'space_id':fresh.space_id,
                        'grant_id':fresh.grant_id,'selected':selected,'fingerprint':request_fingerprint}
                prior=self.store.one('SELECT value FROM meta WHERE key=?',(key,))
                if prior:
                    if json.loads(prior['value'])!=intent:
                        raise DevError('VALIDATION_CHANGED','Validation inputs or resources changed; use a reviewed new save request',409)
                else:
                    self.store.db.execute('INSERT INTO meta(key,value) VALUES(?,?)',(key,json.dumps(intent,sort_keys=True)))
                return intent,None
        intent,immediate=await self.store.run(prepare)
        if immediate is not None:
            return immediate
        results=[]
        for index,view in enumerate(intent['selected']['views']):
            context={'key':key,'index':index}
            pseudo={**view,'_validation_context':context}
            result=await self.runtime.dispatch('system_validate',{'idempotency_key':'validate-'+fingerprint([key,index])},pseudo,principal)
            if result.get('pending'):
                raise DevError('VALIDATION_PENDING','Continue this same save after the original directory validation completes',409,
                               operation_id=result['operation_id'],retryable=True)
            if not result.get('root') or view['mode']=='write' and not result.get('writable') or view['allow_tasks'] and not result.get('allow_tasks'):
                raise DevError('LOCAL_CAPABILITY_DENIED','The device does not authorize the requested folder capabilities',403,
                               operation_id=result.get('operation_id'))
            if str(absolute_path(result['root']))!=view['root']:
                raise DevError('VALIDATION_PATH_CHANGED','The device resolved a different root; use its canonical path explicitly',409)
            results.append(result)
        def finish():
            with self.store.transaction():
                fresh=reauthenticate() if reauthenticate else iam.live_principal(self.store,principal)
                if (fresh.user_id,fresh.space_id,fresh.grant_id)!=(principal.user_id,principal.space_id,principal.grant_id):
                    raise DevError('IDENTITY_CHANGED','The identity changed during validation',403)
                latest=self._prepare_validation(kind,fresh,payload,identifier)
                if latest!=intent['selected']:
                    raise DevError('VALIDATION_CHANGED','Resources changed during validation; no resource was saved',409)
                selected=self._commit_validated(kind,fresh,payload,identifier,results)
                return record(fresh,selected)
        return await self.store.run(finish)

    def require_validation(self, principal, project, tool, *, observe=False):
        context=project.get('_validation_context')
        if not isinstance(context,dict) or set(context)!={'key','index'} or type(context['index']) is not int:
            raise DevError('VALIDATION_CONTEXT_REQUIRED','A trusted workspace validation intent is required',403)
        row=self.store.one('SELECT value FROM meta WHERE key=?',(context['key'],))
        if not row:
            raise DevError('VALIDATION_CONTEXT_REQUIRED','The original validation intent is unavailable',403)
        intent=json.loads(row['value'])
        if (intent['user_id'],intent['space_id'],intent['grant_id'])!=(principal.user_id,principal.space_id,principal.grant_id):
            raise DevError('VALIDATION_IDENTITY_DENIED','The validation belongs to another identity',403)
        index=context['index']
        if tool!='system_validate' or not 0<=index<len(intent['selected']['views']):
            raise DevError('VALIDATION_CONTEXT_INVALID','The validation context does not permit this operation',403)
        if any(project.get(k)!=v for k,v in intent['selected']['views'][index].items() if k!='device_id'):
            raise DevError('VALIDATION_CONTEXT_INVALID','The validation target changed',403)
        if not observe:
            if self._prepare_validation(intent['kind'],principal,intent['payload'],intent['identifier'])!=intent['selected']:
                raise DevError('VALIDATION_CHANGED','The original validation configuration changed',409)
        elif intent['kind'] in {'mapping','pool','folder'}:
            if principal.grant_id:
                raise DevError('PANEL_REQUIRED','Inventory validation requires a panel account',403)
            if intent['kind']=='mapping':
                owned_resource(self.store,principal,'storage_pools',intent['payload']['pool_id'])
        elif principal.grant_id:
            require_access(self.store,principal,'projects.create',source_id=intent['payload']['source_id'])
        else:
            iam.require_space_capability(self.store,principal,'resources')
        return intent['selected']['views'][index]['device_id']

    def _commit_validated(self, kind, principal, payload, identifier, results):
        now=time.time()
        if kind=='pool':
            fields=StoragePoolInput.model_validate({k:payload[k] for k in StoragePoolInput.model_fields})
            selected='pool_'+uuid.uuid4().hex
            name,_=label_key(fields.name)
            self.store.db.execute('''INSERT INTO storage_pools(id,name,abstract_base,owner_user_id,enabled,
                default_fs_device_id,default_shell_device_id,shell_allowed_device_ids,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(selected,name,fields.abstract_base,principal.user_id,int(fields.enabled),
                fields.default_fs_device_id,fields.default_shell_device_id,json.dumps(fields.shell_allowed_device_ids),now,now))
            for mapping,result in zip(payload['mappings'],results,strict=True):
                self.store.db.execute('''INSERT INTO storage_mappings(id,pool_id,device_id,path,validated_at,writable,allow_tasks)
                    VALUES(?,?,?,?,?,?,?)''',('map_'+uuid.uuid4().hex,selected,mapping['device_id'],mapping['path'],now,
                                            int(result['writable']),int(result['allow_tasks'])))
            self._pool_state(selected)
            return public_pool(self.store.one('SELECT * FROM storage_pools WHERE id=?',(selected,)))
        if kind=='mapping':
            selected=identifier or 'map_'+uuid.uuid4().hex
            fields=StorageMappingInput.model_validate({k:payload[k] for k in StorageMappingInput.model_fields})
            try:
                if identifier:
                    self.store.db.execute('UPDATE storage_mappings SET path=?,enabled=?,validated_at=?,writable=?,allow_tasks=?,version=version+1 WHERE id=?',
                        (fields.path,int(fields.enabled),now,int(results[0]['writable']),int(results[0]['allow_tasks']),selected))
                else:
                    self.store.db.execute('INSERT INTO storage_mappings(id,pool_id,device_id,path,enabled,validated_at,writable,allow_tasks) VALUES(?,?,?,?,?,?,?,?)',
                        (selected,payload['pool_id'],fields.device_id,fields.path,int(fields.enabled),now,int(results[0]['writable']),int(results[0]['allow_tasks'])))
            except sqlite3.IntegrityError as exc:
                raise DevError('MAPPING_CONFLICT','The Pool already has a mapping for this device',409) from exc
            self._pool_state(payload['pool_id'])
            self.validate_affected(pool_id=payload['pool_id'])
            return self.store.one('SELECT * FROM storage_mappings WHERE id=?',(selected,))
        if kind=='folder':
            fields=FolderInput.model_validate({k:payload[k] for k in FolderInput.model_fields})
            selected=identifier or 'fld_'+uuid.uuid4().hex
            name,_=label_key(fields.name)
            values=(name,fields.device_path,fields.mode,int(fields.allow_tasks),int(fields.enabled),now,now)
            if identifier:
                self.store.db.execute('UPDATE folders SET name=?,device_path=?,mode=?,allow_tasks=?,enabled=?,validated_at=?,updated=?,version=version+1 WHERE id=?',(*values,selected))
            else:
                self.store.db.execute('''INSERT INTO folders(name,device_path,mode,allow_tasks,enabled,validated_at,updated,id,device_id,owner_user_id,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(*values,selected,fields.device_id,principal.user_id,now))
            if fields.enabled:
                self.validate_affected(folder_resource_id=selected)
            return self.store.one('SELECT * FROM folders WHERE id=?',(selected,))
        if kind=='source':
            fields=SourceInput.model_validate({k:payload[k] for k in SourceInput.model_fields})
            selected=identifier or 'src_'+uuid.uuid4().hex
            name,_=label_key(fields.name)
            values=(name,fields.subpath,fields.mode,int(fields.allow_tasks),fields.files.model_dump_json(),
                    fields.shell.model_dump_json(),int(fields.enabled),now)
            try:
                if identifier:
                    self.store.db.execute('UPDATE space_sources SET name=?,subpath=?,mode=?,allow_tasks=?,files=?,shell=?,enabled=?,validated_at=?,version=version+1 WHERE id=?',(*values,selected))
                    self.store.db.execute('DELETE FROM space_source_mappings WHERE source_id=?',(selected,))
                else:
                    self.store.db.execute('''INSERT INTO space_sources(name,subpath,mode,allow_tasks,files,shell,enabled,validated_at,id,space_id,source_kind,folder_resource_id,pool_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(*values,selected,principal.space_id,fields.source_kind,fields.folder_resource_id,fields.pool_id))
                if fields.source_kind=='storage':
                    mappings=self.store.all('SELECT * FROM storage_mappings WHERE pool_id=? AND enabled=1 ORDER BY id',(fields.pool_id,))
                    for mapping in mappings:
                        if not fields.mapping_ids or mapping['id'] in fields.mapping_ids:
                            self.store.db.execute('INSERT INTO space_source_mappings VALUES(?,?,?,?,?)',
                                (principal.space_id,selected,fields.pool_id,mapping['id'],mapping['device_id']))
            except sqlite3.IntegrityError as exc:
                raise DevError('SOURCE_CONFLICT','The source name exists or its resource mappings changed',409) from exc
            if fields.enabled:
                self.validate_affected(principal.space_id,source_id=selected)
            return self.source_public(self.store.one('SELECT * FROM space_sources WHERE id=?',(selected,)))
        fields=ProjectInput.model_validate({k:payload[k] for k in ProjectInput.model_fields})
        selected=identifier or uuid.uuid4().hex
        alias,key=label_key(fields.alias)
        try:
            if identifier:
                self.store.db.execute('UPDATE projects SET alias=?,alias_key=?,description=?,source_id=?,subpath=?,version=version+1 WHERE id=?',
                    (alias,key,fields.description,fields.source_id,fields.subpath,selected))
            else:
                self.store.db.execute('INSERT INTO projects(id,space_id,alias,alias_key,source_id,subpath,description,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?,?)',
                    (selected,principal.space_id,alias,key,fields.source_id,fields.subpath,fields.description,principal.user_id,now))
        except sqlite3.IntegrityError as exc:
            raise DevError('PROJECT_CONFLICT','The alias exists or the source is not provided by this workspace',409) from exc
        self.validate_affected(principal.space_id,source_id=fields.source_id)
        return self.store.one('SELECT * FROM projects WHERE id=?',(selected,))

    def source_public(self, row):
        result=public_source(row)
        result['mappings']=self.store.all('''SELECT m.*,d.name AS device_name FROM space_source_mappings sm
            JOIN storage_mappings m ON m.id=sm.mapping_id JOIN devices d ON d.id=m.device_id
            WHERE sm.source_id=? ORDER BY d.name,m.id''',(row['id'],))
        result['mapping_ids']=[m['id'] for m in result['mappings']]
        pseudo={'id':'','space_id':row['space_id'],'source_id':row['id'],'subpath':'.'}
        try:
            source=source_views(self.store,pseudo)
            result['views']=[{'device_id':v['device_id'],'device_name':v['device_name'],'root':v['root'],
                'logical_root':v['logical_root'],'shell_capable':v['allow_tasks'] and v['mode']=='write' and v['shell_enabled'] is not False}
                for v in source['views'].values()]
            result['files_device_id']=source['files_device_id']
            result['default_device_id']=source['default_device_id']
            result['allowed_device_ids']=sorted(source['allowed_device_ids'])
            result['shell_origin']=source['shell_origin']
        except DevError as exc:
            result.update(views=[],unavailable={'code':exc.code,'message':exc.message})
        return result

    def save_shell(self, principal, body, project_id):
        def save(current):
            iam.require_space_capability(self.store,current,'resources')
            project=iam.project_in_space(self.store,current,project_id)
            old=self.store.one('SELECT * FROM project_shell_settings WHERE project_id=?',(project_id,))
            checked_version(old,body.expected_version)
            validate_project_shell(self.store,project,body.settings)
            self.store.db.execute('''INSERT INTO project_shell_settings(project_id,settings) VALUES(?,?)
                ON CONFLICT(project_id) DO UPDATE SET settings=excluded.settings,version=project_shell_settings.version+1''',
                (project_id,body.settings.model_dump_json()))
            self.validate_affected(current.space_id,source_id=project['source_id'])
            self.changed(current,project_id)
            row=self.store.one('SELECT * FROM project_shell_settings WHERE project_id=?',(project_id,))
            return {**row,'settings':json.loads(row['settings'])}
        return mutate(self.store,principal,'shell.save:'+project_id,body.idempotency_key,body.model_dump(),save)
