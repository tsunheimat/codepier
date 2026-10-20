"""Workspace resources and durable, device-validated folder/project mutations.

No live database migration, operating-system mount or filesystem creation occurs.
The validation intent pins its original inputs through waits and restart/retries.
"""
from __future__ import annotations

import asyncio
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
                                   validate_project_shell, select_view, _device)
from shared.workspace_contracts import (StoragePoolInput, StorageMappingInput, SpaceStorageInput,
    FolderInput, ProjectInput, ShellSettings, absolute_path, mapped_path, relative_path)
from shared.util import DevError


def checked_version(row, expected):
    if type(expected) is not int or (row['version'] if row else 0) != expected:
        raise DevError('VERSION_CHANGED', 'Configuration changed; reload it before saving', 409)


def owned_resource(store, principal, table, identifier):
    if table not in {'devices', 'storage_pools', 'vps_connections', 'gateway_connectors'}:
        raise ValueError('Unknown inventory resource type')
    principal = iam.live_principal(store, principal)
    row = store.one('SELECT * FROM '+table+' WHERE id=?', (identifier,))
    if (not row or principal.grant_id or not (principal.instance_admin or row['owner_user_id']==principal.user_id)):
        raise DevError('INVENTORY_RESOURCE_NOT_FOUND', 'Inventory resource is not owned by this account', 404)
    return row


def public_storage(row):
    return {**row, 'allowed_device_ids': json.loads(row['allowed_device_ids']),
            'allowed_mode': 'selected' if json.loads(row['allowed_device_ids']) else 'all_source_devices'}


class WorkspaceResources:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def changed(self, principal, identifier):
        self.store.after_commit(lambda: self.runtime.publish('project', {'id': identifier, 'space_id': principal.space_id}))
        self.runtime.wake_delivery()

    def inventory(self, principal, table):
        if table not in {'storage_pools', 'vps_connections'}:
            raise ValueError('Unknown inventory')
        principal = iam.live_principal(self.store, principal)
        if principal.grant_id:
            raise DevError('PANEL_REQUIRED', 'Inventory requires a panel account', 403)
        return self.store.all('SELECT * FROM '+table+(' ORDER BY name,id' if principal.instance_admin else ' WHERE owner_user_id=? ORDER BY name,id'),
                              () if principal.instance_admin else (principal.user_id,))

    def save_pool(self, principal, body, identifier=None):
        fields = StoragePoolInput.model_validate(body.model_dump(include=set(StoragePoolInput.model_fields)))
        def save(current):
            old = owned_resource(self.store, current, 'storage_pools', identifier) if identifier else None
            checked_version(old, body.expected_version)
            name, _ = label_key(fields.name)
            selected = identifier or 'pool_'+uuid.uuid4().hex
            # Avoid ambiguous abstract prefixes: mapping is by source identity,
            # but a human-readable namespace must still be unambiguous.
            base = absolute_path(fields.abstract_base)
            for other in self.store.all('SELECT id,abstract_base FROM storage_pools WHERE id<>?', (selected,)):
                candidate = absolute_path(other['abstract_base'])
                if base == candidate or base in candidate.parents or candidate in base.parents:
                    raise DevError('ABSTRACT_PATH_CONFLICT', 'Abstract pool bases cannot overlap', 409)
            now = time.time()
            if old:
                self.store.db.execute('UPDATE storage_pools SET name=?,abstract_base=?,enabled=?,version=version+1,updated=? WHERE id=?',
                    (name, fields.abstract_base, int(fields.enabled), now, selected))
            else:
                self.store.db.execute('INSERT INTO storage_pools(id,name,abstract_base,owner_user_id,enabled,created,updated) VALUES(?,?,?,?,?,?,?)',
                    (selected, name, fields.abstract_base, current.user_id, int(fields.enabled), now, now))
            self.changed(current, selected)
            return self.store.one('SELECT * FROM storage_pools WHERE id=?', (selected,))
        return mutate(self.store, principal, 'storage-pool.save:'+(identifier or 'new'), body.idempotency_key, body.model_dump(), save)

    def bind_device(self, principal, device_id, body):
        def save(current):
            iam.require_space_capability(self.store, current, 'resources')
            owned_resource(self.store, replace(current, space_id=None), 'devices', device_id)
            row = self.store.one('SELECT * FROM space_devices WHERE space_id=? AND device_id=?', (current.space_id, device_id))
            checked_version(row, body.expected_version)
            if not body.enabled:
                in_use = self.store.one('''SELECT id FROM space_folders WHERE space_id=? AND device_id=? AND enabled=1
                    UNION SELECT id FROM space_vps WHERE space_id=? AND runner_device_id=? AND enabled=1
                    UNION SELECT storage_binding_id FROM space_storage_mappings WHERE space_id=? AND device_id=? LIMIT 1''',
                    (current.space_id,device_id,current.space_id,device_id,current.space_id,device_id))
                if in_use:
                    raise DevError('DEVICE_BINDING_IN_USE', 'Remove the workspace folder, storage or VPS references before disabling this binding', 409)
            self.store.db.execute('''INSERT INTO space_devices(space_id,device_id,enabled) VALUES(?,?,?)
                ON CONFLICT(space_id,device_id) DO UPDATE SET enabled=excluded.enabled,version=space_devices.version+1''',
                (current.space_id,device_id,int(body.enabled)))
            self.changed(current,device_id)
            return self.store.one('SELECT * FROM space_devices WHERE space_id=? AND device_id=?',(current.space_id,device_id))
        return mutate(self.store,principal,'device.bind:'+device_id,body.idempotency_key,body.model_dump(),save)

    def _storage_selection(self, principal, fields):
        pool = owned_resource(self.store, replace(principal,space_id=None), 'storage_pools', fields.pool_id)
        if not pool['enabled']:
            raise DevError('STORAGE_POOL_DISABLED','Enable the inventory pool before binding it',409)
        mappings = []
        for identifier in fields.mapping_ids:
            row = self.store.one('SELECT * FROM storage_mappings WHERE id=? AND pool_id=? AND enabled=1', (identifier,fields.pool_id))
            if not row or row['validated_at'] is None:
                raise DevError('MAPPING_NOT_VALIDATED','Choose validated mappings from this pool',409)
            device = _device(self.store,principal.space_id,row['device_id'])
            mappings.append((row,device))
        mapped = {m['device_id'] for m,_ in mappings}
        executable = {m['device_id'] for m,d in mappings if m['allow_tasks'] and m['writable'] and json.loads(d['info']).get('shell_enabled') is not False}
        if fields.files_device_id not in mapped or not set(fields.allowed_device_ids) <= executable:
            raise DevError('DEVICE_NOT_IN_SOURCE','Choose only capabilities provided by the selected workspace mappings',403)
        allowed = set(fields.allowed_device_ids) if fields.allowed_device_ids else executable
        if fields.default_device_id not in allowed:
            raise DevError('DEFAULT_DEVICE_REQUIRED','The default must be an executable device among these workspace mappings',409)
        return pool,[m for m,_ in mappings]

    def validate_affected(self, space_id, *, folder_id=None, storage_id=None):
        sql='SELECT p.* FROM projects p JOIN space_folders f ON f.id=p.folder_id WHERE p.space_id=? AND f.enabled=1'
        args=[space_id]
        if folder_id:
            sql+=' AND f.id=?';args.append(folder_id)
        if storage_id:
            sql+=' AND f.storage_binding_id=?';args.append(storage_id)
        conflicts=[]
        for project in self.store.all(sql,args):
            try:
                source=source_views(self.store,project)
                if source['files_device_id'] not in source['views']:
                    raise DevError('FILES_DEVICE_UNAVAILABLE','The project needs its files mapping',409)
                if source['folder']['mode']=='write' and source['views'][source['files_device_id']]['mode']!='write':
                    raise DevError('FILES_READ_ONLY','The selected files mapping is not writable',403)
                settings=shell_settings(self.store,project)
                if source['folder']['allow_tasks'] and not any(s.mode=='disabled' for s in settings):
                    resolve_shell(self.store,project,source=source)
            except DevError as exc:
                conflicts.append({'project_id':project['id'],'alias':project['alias'],'code':exc.code})
        if conflicts:
            raise DevError('WORKSPACE_CONFIGURATION_CONFLICT','This edit would leave projects without their required source/default; update their settings first',409,conflicts=conflicts)

    def save_storage_binding(self, principal, body, identifier=None):
        fields=SpaceStorageInput.model_validate(body.model_dump(include=set(SpaceStorageInput.model_fields)))
        def save(current):
            iam.require_space_capability(self.store,current,'resources')
            old=self.store.one('SELECT * FROM space_storage WHERE id=? AND space_id=?',(identifier,current.space_id)) if identifier else None
            if identifier and not old:
                raise DevError('STORAGE_BINDING_NOT_FOUND','Workspace storage binding not found',404)
            checked_version(old,body.expected_version)
            if old and old['pool_id']!=fields.pool_id:
                raise DevError('STORAGE_IDENTITY_IMMUTABLE','Create another binding instead of retargeting its pool identity',409)
            _,mappings=self._storage_selection(current,fields)
            selected=identifier or 'stb_'+uuid.uuid4().hex
            name,_=label_key(fields.name)
            try:
                if old:
                    self.store.db.execute('UPDATE space_storage SET name=?,default_device_id=?,files_device_id=?,allowed_device_ids=?,enabled=?,version=version+1 WHERE id=?',
                        (name,fields.default_device_id,fields.files_device_id,json.dumps(fields.allowed_device_ids),int(fields.enabled),selected))
                    self.store.db.execute('DELETE FROM space_storage_mappings WHERE storage_binding_id=?',(selected,))
                else:
                    self.store.db.execute('INSERT INTO space_storage(id,space_id,pool_id,name,default_device_id,files_device_id,allowed_device_ids,enabled) VALUES(?,?,?,?,?,?,?,?)',
                        (selected,current.space_id,fields.pool_id,name,fields.default_device_id,fields.files_device_id,json.dumps(fields.allowed_device_ids),int(fields.enabled)))
                for mapping in mappings:
                    self.store.db.execute('INSERT INTO space_storage_mappings VALUES(?,?,?,?,?)',
                        (current.space_id,selected,fields.pool_id,mapping['id'],mapping['device_id']))
            except sqlite3.IntegrityError as exc:
                raise DevError('STORAGE_BINDING_CONFLICT','This pool is already bound or a selected mapping is not in the workspace',409) from exc
            if fields.enabled:
                self.validate_affected(current.space_id,storage_id=selected)
            self.changed(current,selected)
            return public_storage(self.store.one('SELECT * FROM space_storage WHERE id=?',(selected,)))
        return mutate(self.store,principal,'storage.bind:'+(identifier or 'new'),body.idempotency_key,body.model_dump(),save)

    def _folder_views(self, principal, fields):
        if fields.source_kind=='device':
            device=_device(self.store,principal.space_id,fields.device_id)
            return [{'device_id':device['id'],'root':fields.device_path,'alias':'folder-validation','mode':fields.mode,
                     'allow_tasks':fields.allow_tasks,'_source_boundary':{'mount':fields.device_path,'subpath':'.','protocol':1}}], {'device':device['id']}
        binding=self.store.one('SELECT * FROM space_storage WHERE id=? AND space_id=? AND enabled=1',(fields.storage_binding_id,principal.space_id))
        if not binding:
            raise DevError('STORAGE_BINDING_NOT_FOUND','Choose a storage binding in this workspace',404)
        rows=self.store.all('''SELECT m.* FROM space_storage_mappings sm JOIN storage_mappings m ON m.id=sm.mapping_id
            WHERE sm.space_id=? AND sm.storage_binding_id=? AND m.enabled=1''',(principal.space_id,binding['id']))
        views=[]
        for mapping in rows:
            _device(self.store,principal.space_id,mapping['device_id'])
            writable=fields.mode=='write' and mapping['writable']
            if mapping['device_id']==binding['files_device_id'] and fields.mode=='write' and not writable:
                raise DevError('FILES_READ_ONLY','The files mapping cannot provide a writable folder',403)
            views.append({'device_id':mapping['device_id'],'root':mapped_path(mapping['path'],fields.subpath),
                'alias':'folder-validation','mode':'write' if writable else 'read',
                'allow_tasks':bool(fields.allow_tasks and mapping['allow_tasks']),
                '_source_boundary':{'mount':mapping['path'],'subpath':fields.subpath,'protocol':1}})
        if not views:
            raise DevError('STORAGE_MAPPING_MISSING','The workspace storage binding has no enabled mappings',409)
        return views, {'binding':binding,'mappings':rows}

    def _prepare_validation(self, kind, principal, payload, identifier):
        principal=iam.live_principal(self.store,principal)
        if kind=='mapping':
            fields=StorageMappingInput.model_validate({k:payload[k] for k in StorageMappingInput.model_fields})
            pool=owned_resource(self.store,principal,'storage_pools',payload['pool_id'])
            device=owned_resource(self.store,principal,'devices',fields.device_id)
            if not iam.device_identity_active(self.store,device):
                raise DevError('DEVICE_DISABLED','The mapping device is disabled',409)
            old=self.store.one('SELECT * FROM storage_mappings WHERE id=? AND pool_id=?',(identifier,pool['id'])) if identifier else None
            if identifier and not old:
                raise DevError('MAPPING_NOT_FOUND','Mapping not found',404)
            checked_version(old,payload['expected_version'])
            if old and old['device_id']!=fields.device_id:
                raise DevError('MAPPING_IDENTITY_IMMUTABLE','Create another mapping to change its device',409)
            views=[{'device_id':fields.device_id,'root':fields.path,'alias':'mapping-validation','mode':'read','allow_tasks':False,
                    '_source_boundary':{'mount':fields.path,'subpath':'.','protocol':1}}]
            return {'before':old,'pool':pool,'views':views}
        if kind=='folder':
            iam.require_space_capability(self.store,principal,'resources')
            fields=FolderInput.model_validate({k:payload[k] for k in FolderInput.model_fields})
            old=self.store.one('SELECT * FROM space_folders WHERE id=? AND space_id=?',(identifier,principal.space_id)) if identifier else None
            if identifier and not old:
                raise DevError('FOLDER_NOT_FOUND','Workspace folder not found',404)
            checked_version(old,payload['expected_version'])
            views,source=self._folder_views(principal,fields)
            return {'before':old,'source':source,'views':views}
        if kind=='project':
            fields=ProjectInput.model_validate({k:payload[k] for k in ProjectInput.model_fields})
            if principal.grant_id:
                if identifier:
                    raise DevError('PROJECT_CONFIGURATION_DENIED','MCP cannot reconfigure an existing project',403)
                require_access(self.store,principal,'projects.create',folder_id=fields.folder_id)
            else:
                iam.require_space_capability(self.store,principal,'resources')
            old=self.store.one('SELECT * FROM projects WHERE id=? AND space_id=?',(identifier,principal.space_id)) if identifier else None
            if identifier and not old:
                raise DevError('PROJECT_NOT_FOUND','Project not found',404)
            checked_version(old,payload['expected_version'])
            candidate={'id':identifier or '', 'space_id':principal.space_id,'folder_id':fields.folder_id,'subpath':fields.subpath}
            source=source_views(self.store,candidate)
            if fields.alias.strip() in {'.','..'} or not re.fullmatch(r'[\w.-]{1,64}',fields.alias.strip()):
                raise DevError('INVALID_ALIAS','Use a project name, not a path or whitespace')
            occupied=self.store.one('SELECT id FROM projects WHERE space_id=? AND alias_key=?',(principal.space_id,label_key(fields.alias)[1]))
            if occupied and occupied['id']!=identifier:
                raise DevError('ALIAS_EXISTS','A project with this alias already exists in the workspace',409)
            views=[{'device_id':v['device_id'],'root':v['root'],'alias':fields.alias,'mode':v['mode'],'allow_tasks':v['allow_tasks'],
                    '_source_boundary':{'mount':v['mount'],'subpath':v['subpath'],'protocol':1}} for v in source['views'].values()]
            if source['folder']['allow_tasks'] and not any(s.mode=='disabled' for s in shell_settings(self.store,candidate)):
                resolve_shell(self.store,candidate,source=source)
            if not views:
                raise DevError('SOURCE_UNAVAILABLE','This workspace folder has no enabled source views',409)
            # Sets are converted to canonical JSON; identities/paths are pinned across awaits.
            source['allowed_device_ids']=sorted(source['allowed_device_ids'])
            return {'before':old,'source':source,'views':views}
        raise DevError('INVALID_VALIDATION','Unsupported workspace validation intent')

    async def validated_save(self, kind, principal, body, identifier=None, *, pool_id=None, reauthenticate=None):
        payload=body.model_dump()
        if pool_id is not None:
            payload['pool_id']=pool_id
        operation=kind+'.save:'+(identifier or 'new')+(':'+pool_id if pool_id else '')
        request_fingerprint=fingerprint(payload)
        key='workspace-validation:'+fingerprint([principal.space_id,principal.user_id,principal.grant_id,operation,body.idempotency_key])
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
                    if kind=='mapping':
                        owned_resource(self.store,fresh,'storage_pools',pool_id)
                    elif fresh.grant_id:
                        require_access(self.store,fresh,'projects.create',folder_id=payload['folder_id'])
                    else:
                        iam.require_space_capability(self.store,fresh,'resources')
                    return None,json.loads(old['receipt'])
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
        intent,replayed=await self.store.run(prepare)
        if replayed is not None:
            return {**replayed,'replayed':True}
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
                receipt=json.dumps(selected,ensure_ascii=False)
                self.store.db.execute('INSERT INTO workspace_requests VALUES(?,?,?,?,?,?,?)',
                    (fresh.space_id or '',fresh.user_id,operation,body.idempotency_key,request_fingerprint,receipt,time.time()))
                iam.audit(self.store,fresh,operation,selected['id'],detail={'version':selected.get('version')})
                self.changed(fresh,selected['id'])
                return selected
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
        elif intent['kind']=='mapping':
            owned_resource(self.store,principal,'storage_pools',intent['payload']['pool_id'])
        elif principal.grant_id:
            require_access(self.store,principal,'projects.create',folder_id=intent['payload']['folder_id'])
        else:
            iam.require_space_capability(self.store,principal,'resources')
        return intent['selected']['views'][index]['device_id']

    def _commit_validated(self, kind, principal, payload, identifier, results):
        now=time.time()
        if kind=='mapping':
            selected=identifier or 'map_'+uuid.uuid4().hex
            fields={k:payload[k] for k in StorageMappingInput.model_fields}
            try:
                if identifier:
                    self.store.db.execute('UPDATE storage_mappings SET path=?,enabled=?,validated_at=?,writable=?,allow_tasks=?,version=version+1 WHERE id=?',
                        (fields['path'],int(fields['enabled']),now,int(results[0]['writable']),int(results[0]['allow_tasks']),selected))
                else:
                    self.store.db.execute('INSERT INTO storage_mappings(id,pool_id,device_id,path,enabled,validated_at,writable,allow_tasks) VALUES(?,?,?,?,?,?,?,?)',
                        (selected,payload['pool_id'],fields['device_id'],fields['path'],int(fields['enabled']),now,int(results[0]['writable']),int(results[0]['allow_tasks'])))
            except sqlite3.IntegrityError as exc:
                raise DevError('MAPPING_CONFLICT','The pool already has a mapping for this device',409) from exc
            for binding in self.store.all('SELECT id,space_id FROM space_storage WHERE pool_id=? AND enabled=1',(payload['pool_id'],)):
                self.validate_affected(binding['space_id'],storage_id=binding['id'])
            return self.store.one('SELECT * FROM storage_mappings WHERE id=?',(selected,))
        if kind=='folder':
            fields=FolderInput.model_validate({k:payload[k] for k in FolderInput.model_fields})
            selected=identifier or 'fld_'+uuid.uuid4().hex
            name,_=label_key(fields.name)
            values=(name,fields.source_kind,fields.device_id,fields.device_path,fields.storage_binding_id,fields.subpath,fields.mode,int(fields.allow_tasks),int(fields.enabled),now)
            try:
                if identifier:
                    self.store.db.execute('UPDATE space_folders SET name=?,source_kind=?,device_id=?,device_path=?,storage_binding_id=?,subpath=?,mode=?,allow_tasks=?,enabled=?,validated_at=?,version=version+1 WHERE id=?',(*values,selected))
                else:
                    self.store.db.execute('INSERT INTO space_folders(name,source_kind,device_id,device_path,storage_binding_id,subpath,mode,allow_tasks,enabled,validated_at,id,space_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(*values,selected,principal.space_id))
            except sqlite3.IntegrityError as exc:
                raise DevError('FOLDER_CONFLICT','This folder name already exists or its source is not in the workspace',409) from exc
            if fields.enabled:
                self.validate_affected(principal.space_id,folder_id=selected)
            return self.store.one('SELECT * FROM space_folders WHERE id=?',(selected,))
        fields=ProjectInput.model_validate({k:payload[k] for k in ProjectInput.model_fields})
        selected=identifier or uuid.uuid4().hex
        alias,key=label_key(fields.alias)
        try:
            if identifier:
                self.store.db.execute('UPDATE projects SET alias=?,alias_key=?,description=?,folder_id=?,subpath=?,version=version+1 WHERE id=?',
                    (alias,key,fields.description,fields.folder_id,fields.subpath,selected))
            else:
                self.store.db.execute('INSERT INTO projects(id,space_id,alias,alias_key,folder_id,subpath,description,owner_user_id,created) VALUES(?,?,?,?,?,?,?,?,?)',
                    (selected,principal.space_id,alias,key,fields.folder_id,fields.subpath,fields.description,principal.user_id,now))
        except sqlite3.IntegrityError as exc:
            raise DevError('PROJECT_CONFLICT','The alias exists or the folder is not provided by this workspace',409) from exc
        self.validate_affected(principal.space_id,folder_id=fields.folder_id)
        return self.store.one('SELECT * FROM projects WHERE id=?',(selected,))

    def save_shell(self, principal, body, project_id=None):
        def save(current):
            iam.require_space_capability(self.store,current,'resources')
            project=iam.project_in_space(self.store,current,project_id) if project_id else None
            table,key,value=('project_shell_settings','project_id',project_id) if project else ('space_shell_settings','space_id',current.space_id)
            old=self.store.one('SELECT * FROM '+table+' WHERE '+key+'=?',(value,))
            checked_version(old,body.expected_version)
            if project:
                validate_project_shell(self.store,project,body.settings)
            elif body.settings.mode=='custom':
                for device_id in {body.settings.default_device_id,*body.settings.allowed_device_ids}:
                    _device(self.store,current.space_id,device_id)
            self.store.db.execute('INSERT INTO '+table+'('+key+',settings) VALUES(?,?) ON CONFLICT('+key+') DO UPDATE SET settings=excluded.settings,version='+table+'.version+1',
                (value,body.settings.model_dump_json()))
            self.validate_affected(current.space_id,folder_id=project['folder_id'] if project else None)
            self.changed(current,project_id or current.space_id)
            row=self.store.one('SELECT * FROM '+table+' WHERE '+key+'=?',(value,))
            return {**row,'settings':json.loads(row['settings'])}
        return mutate(self.store,principal,'shell.save:'+(project_id or 'workspace'),body.idempotency_key,body.model_dump(),save)
