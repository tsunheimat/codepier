"""Explicit workspace URLs and global inventory; no active-space headers."""
from __future__ import annotations

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field

from hub import iam
from hub.db_worker import database_endpoint
from hub.permissions import require_access
from hub.workspace_mutations import mutate
from hub.workspace_resources import WorkspaceResources, owned_resource, checked_version, public_storage
from hub.workspace_sources import source_views, select_view, fingerprint
from shared.workspace_contracts import (WorkspaceModel, StoragePoolInput, StorageMappingInput,
    SpaceStorageInput, FolderInput, ProjectInput, ShellSettings, relative_path)
from shared.routing_contracts import SpaceExecutionSettings
from shared.util import DevError


class Change(WorkspaceModel):
    expected_version: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=8, max_length=128)


class PoolSave(StoragePoolInput, Change):
    pass


class MappingSave(StorageMappingInput, Change):
    confirmed_same_storage: Literal[True]


class StorageSave(SpaceStorageInput, Change):
    pass


class FolderSave(FolderInput, Change):
    pass


class ProjectSave(ProjectInput, Change):
    pass


class DeviceBindingSave(Change):
    enabled: bool = True


class ShellSave(Change):
    settings: ShellSettings


class ExecutionSave(Change):
    settings: SpaceExecutionSettings


class ReleaseHold(WorkspaceModel):
    confirmed_remote_stopped: Literal[True]
    expected_state: Literal['needs_review']


def make_workspace_router(auth, runtime):
    router,store=APIRouter(),runtime.store
    resources=runtime.workspace_resources

    @router.get('/api/storage-pools')
    @database_endpoint(store)
    def pools(request: Request):
        principal=auth.panel(request)
        return {'pools':resources.inventory(principal,'storage_pools')}

    @router.post('/api/storage-pools',status_code=201)
    @database_endpoint(store)
    def create_pool(request: Request,body: PoolSave):
        return resources.save_pool(auth.panel(request,True),body)

    @router.put('/api/storage-pools/{pool_id}')
    @database_endpoint(store)
    def update_pool(pool_id: str,request: Request,body: PoolSave):
        return resources.save_pool(auth.panel(request,True),body,pool_id)

    @router.get('/api/storage-pools/{pool_id}/mappings')
    @database_endpoint(store)
    def mappings(pool_id: str,request: Request):
        owned_resource(store,auth.panel(request),'storage_pools',pool_id)
        return {'mappings':store.all('''SELECT m.*,d.name AS device_name FROM storage_mappings m
            JOIN devices d ON d.id=m.device_id WHERE m.pool_id=? ORDER BY d.name,m.id''',(pool_id,))}

    @router.post('/api/storage-pools/{pool_id}/mappings',status_code=201)
    async def create_mapping(pool_id: str,request: Request,body: MappingSave):
        principal=await store.run(auth.panel,request,True)
        return await resources.validated_save('mapping',principal,body,pool_id=pool_id,
                                             reauthenticate=lambda:auth.panel(request,True))

    @router.put('/api/storage-pools/{pool_id}/mappings/{mapping_id}')
    async def update_mapping(pool_id: str,mapping_id: str,request: Request,body: MappingSave):
        principal=await store.run(auth.panel,request,True)
        return await resources.validated_save('mapping',principal,body,mapping_id,pool_id=pool_id,
                                             reauthenticate=lambda:auth.panel(request,True))

    @router.delete('/api/storage-pools/{pool_id}/mappings/{mapping_id}')
    @database_endpoint(store)
    def remove_mapping(pool_id: str,mapping_id: str,request: Request,expected_version: int=Query(ge=1)):
        with store.transaction():
            principal=auth.panel(request,True)
            owned_resource(store,principal,'storage_pools',pool_id)
            row=store.one('SELECT * FROM storage_mappings WHERE id=? AND pool_id=?',(mapping_id,pool_id))
            if not row:
                raise DevError('MAPPING_NOT_FOUND','Mapping not found',404)
            checked_version(row,expected_version)
            if store.one('SELECT mapping_id FROM space_storage_mappings WHERE mapping_id=? LIMIT 1',(mapping_id,)):
                raise DevError('MAPPING_IN_USE','Remove this mapping from its workspace storage bindings first',409)
            store.db.execute('DELETE FROM storage_mappings WHERE id=?',(mapping_id,))
            iam.audit(store,principal,'storage.mapping_removed',mapping_id,detail={'files_deleted':False})
        return {'id':mapping_id,'files_deleted':False}

    @router.delete('/api/storage-pools/{pool_id}')
    @database_endpoint(store)
    def remove_pool(pool_id: str,request: Request,expected_version: int=Query(ge=1)):
        with store.transaction():
            principal=auth.panel(request,True)
            row=owned_resource(store,principal,'storage_pools',pool_id)
            checked_version(row,expected_version)
            if store.one('SELECT id FROM space_storage WHERE pool_id=? LIMIT 1',(pool_id,)):
                raise DevError('STORAGE_POOL_IN_USE','Remove this resource from its workspaces first',409)
            store.db.execute('DELETE FROM storage_mappings WHERE pool_id=?',(pool_id,))
            store.db.execute('DELETE FROM storage_pools WHERE id=?',(pool_id,))
            iam.audit(store,principal,'storage.pool_removed',pool_id,detail={'files_deleted':False})
        return {'id':pool_id,'files_deleted':False,'devices_deleted':False}

    @router.get('/api/spaces/{space_id}/devices')
    @database_endpoint(store)
    def workspace_devices(request: Request):
        principal=auth.panel(request)
        from hub.api.devices import device_rows
        return {'devices':device_rows(store,runtime,principal)}

    @router.put('/api/spaces/{space_id}/devices/{device_id}')
    @database_endpoint(store)
    def bind_device(device_id: str,request: Request,body: DeviceBindingSave):
        return resources.bind_device(auth.panel(request,True),device_id,body)

    @router.get('/api/spaces/{space_id}/storage')
    @database_endpoint(store)
    def workspace_storage(request: Request):
        principal=iam.require_space_capability(store,auth.panel(request),'resources')
        result=[]
        for row in store.all('SELECT s.*,p.abstract_base FROM space_storage s JOIN storage_pools p ON p.id=s.pool_id WHERE s.space_id=? ORDER BY s.name,s.id',(principal.space_id,)):
            mappings=store.all('''SELECT m.id,m.device_id,m.path,m.enabled,m.writable,m.allow_tasks,m.validated_at,d.name AS device_name
                FROM space_storage_mappings sm JOIN storage_mappings m ON m.id=sm.mapping_id JOIN devices d ON d.id=m.device_id
                WHERE sm.storage_binding_id=? AND sm.space_id=? ORDER BY d.name,m.id''',(row['id'],principal.space_id))
            result.append({**public_storage(row),'mappings':mappings})
        return {'storage':result}

    @router.post('/api/spaces/{space_id}/storage',status_code=201)
    @database_endpoint(store)
    def bind_storage(request: Request,body: StorageSave):
        return resources.save_storage_binding(auth.panel(request,True),body)

    @router.put('/api/spaces/{space_id}/storage/{binding_id}')
    @database_endpoint(store)
    def update_storage(binding_id: str,request: Request,body: StorageSave):
        return resources.save_storage_binding(auth.panel(request,True),body,binding_id)

    @router.delete('/api/spaces/{space_id}/storage/{binding_id}')
    @database_endpoint(store)
    def remove_storage(binding_id: str,request: Request,expected_version: int=Query(ge=1)):
        with store.transaction():
            principal=iam.require_space_capability(store,auth.panel(request,True),'resources')
            row=store.one('SELECT * FROM space_storage WHERE id=? AND space_id=?',(binding_id,principal.space_id))
            if not row:
                raise DevError('STORAGE_BINDING_NOT_FOUND','Storage binding not found',404)
            checked_version(row,expected_version)
            if store.one('SELECT id FROM space_folders WHERE storage_binding_id=? LIMIT 1',(binding_id,)):
                raise DevError('STORAGE_BINDING_IN_USE','Remove its workspace folders first',409)
            store.db.execute('DELETE FROM space_storage_mappings WHERE storage_binding_id=?',(binding_id,))
            store.db.execute('DELETE FROM space_storage WHERE id=?',(binding_id,))
            iam.audit(store,principal,'workspace.storage_removed',binding_id,detail={'files_deleted':False})
            resources.changed(principal,binding_id)
        return {'id':binding_id,'files_deleted':False,'inventory_deleted':False}

    @router.get('/api/spaces/{space_id}/folders')
    @database_endpoint(store)
    def folders(request: Request):
        principal=auth.panel(request)
        result=[]
        for row in store.all('SELECT * FROM space_folders WHERE space_id=? ORDER BY name,id',(principal.space_id,)):
            try:
                require_access(store,principal,'read',folder_id=row['id'])
            except DevError:
                continue
            pseudo={'id':'','space_id':principal.space_id,'folder_id':row['id'],'subpath':'.'}
            try:
                source=source_views(store,pseudo)
                row['views']=[{'device_id':v['device_id'],'name':v['device_name'],'root':v['root'],
                               'logical_root':v['logical_root'],'shell_capable':v['allow_tasks'] and v['mode']=='write' and v['shell_enabled'] is not False}
                              for v in source['views'].values()]
            except DevError as exc:
                row['views']=[]
                row['source_error']=exc.code
            result.append(row)
        return {'folders':result}

    @router.post('/api/spaces/{space_id}/folders',status_code=201)
    async def create_folder(request: Request,body: FolderSave):
        principal=await store.run(auth.panel,request,True)
        return await resources.validated_save('folder',principal,body,reauthenticate=lambda:auth.panel(request,True))

    @router.put('/api/spaces/{space_id}/folders/{folder_id}')
    async def update_folder(folder_id: str,request: Request,body: FolderSave):
        principal=await store.run(auth.panel,request,True)
        return await resources.validated_save('folder',principal,body,folder_id,reauthenticate=lambda:auth.panel(request,True))

    @router.delete('/api/spaces/{space_id}/folders/{folder_id}')
    @database_endpoint(store)
    def remove_folder(folder_id: str,request: Request,expected_version: int=Query(ge=1)):
        with store.transaction():
            principal=iam.require_space_capability(store,auth.panel(request,True),'resources')
            row=store.one('SELECT * FROM space_folders WHERE id=? AND space_id=?',(folder_id,principal.space_id))
            if not row:
                raise DevError('FOLDER_NOT_FOUND','Folder not found',404)
            checked_version(row,expected_version)
            if store.one('SELECT id FROM projects WHERE folder_id=? LIMIT 1',(folder_id,)):
                raise DevError('FOLDER_IN_USE','Rebind or remove its projects first',409)
            store.db.execute('DELETE FROM space_folders WHERE id=?',(folder_id,))
            iam.audit(store,principal,'workspace.folder_removed',folder_id,detail={'files_deleted':False})
            resources.changed(principal,folder_id)
        return {'id':folder_id,'files_deleted':False}

    @router.get('/api/spaces/{space_id}/folders/{folder_id}/browse')
    async def browse_folder(folder_id: str,request: Request,path: str='.',offset: int=Query(default=0,ge=0,le=100000)):
        path=relative_path(path)
        def prepare():
            principal=auth.panel(request)
            require_access(store,principal,'read',folder_id=folder_id)
            view=select_view(store,{'id':'','space_id':principal.space_id,'folder_id':folder_id,'subpath':'.'})
            return principal,view
        principal,view=await store.run(prepare)
        context={'folder_id':folder_id,'view_hash':fingerprint(view),'device_id':view['device_id']}
        pseudo={'alias':'folder-browser','device_id':view['device_id'],'root':view['root'],'mode':'read','allow_tasks':False,
                '_folder_context':context,'_source_boundary':{'mount':view['mount'],'subpath':view['subpath'],'protocol':1}}
        result=await runtime.dispatch('fs_tree',{'project':'folder-browser','path':path,'depth':1,'offset':offset,'limit':200},pseudo,principal)
        fresh,latest=await store.run(prepare)
        if (fresh.user_id,fresh.space_id)!=(principal.user_id,principal.space_id) or latest!=view:
            raise DevError('FOLDER_CHANGED','The folder changed during browsing; reload the directory',409)
        return result

    @router.get('/api/spaces/{space_id}/shell')
    @database_endpoint(store)
    def workspace_shell(request: Request):
        principal=iam.require_space_capability(store,auth.panel(request),'resources')
        row=store.one('SELECT * FROM space_shell_settings WHERE space_id=?',(principal.space_id,))
        return {'settings':json.loads(row['settings']) if row else ShellSettings().model_dump(),'version':row['version'] if row else 0}

    @router.put('/api/spaces/{space_id}/shell')
    @database_endpoint(store)
    def update_workspace_shell(request: Request,body: ShellSave):
        return resources.save_shell(auth.panel(request,True),body)

    @router.put('/api/projects/{project_id}/shell')
    @database_endpoint(store)
    def update_project_shell(project_id: str,request: Request,body: ShellSave):
        return resources.save_shell(auth.panel(request,True),body,project_id)

    @router.get('/api/projects/{project_id}/shell')
    @database_endpoint(store)
    def project_shell(project_id: str,request: Request):
        principal=iam.require_space_capability(store,auth.panel(request),'resources')
        project=iam.project_in_space(store,principal,project_id)
        row=store.one('SELECT * FROM project_shell_settings WHERE project_id=?',(project_id,))
        result={'settings':json.loads(row['settings']) if row else ShellSettings().model_dump(),'version':row['version'] if row else 0}
        from hub.workspace_sources import resolve_shell
        try:
            resolved=resolve_shell(store,project)
            result['resolved']={**resolved,'allowed_device_ids':sorted(resolved['allowed_device_ids'])}
            source=source_views(store,project)
            result['devices']=[{'id':v['device_id'],'name':v['device_name']} for v in source['views'].values() if v['device_id'] in resolved['allowed_device_ids']]
        except DevError as exc:
            result['resolved_error']={'code':exc.code,'message':exc.message}
        return result

    @router.get('/api/spaces/{space_id}/execution')
    @router.get('/api/projects/{project_id}/execution')
    @database_endpoint(store)
    def execution(request: Request):
        principal=iam.require_space_capability(store,auth.panel(request),'resources')
        project_id=request.path_params.get('project_id')
        if project_id:
            iam.project_in_space(store,principal,project_id)
        table,column,identifier=('project_execution_settings','project_id',project_id) if project_id else ('space_execution_settings','space_id',principal.space_id)
        row=store.one('SELECT * FROM '+table+' WHERE '+column+'=?',(identifier,))
        return {'settings':json.loads(row['settings']) if row else SpaceExecutionSettings().model_dump(),'version':row['version'] if row else 0}

    @router.put('/api/spaces/{space_id}/execution')
    @router.put('/api/projects/{project_id}/execution')
    @database_endpoint(store)
    def update_execution(request: Request,body: ExecutionSave):
        principal=iam.require_space_capability(store,auth.panel(request,True),'resources')
        project_id=request.path_params.get('project_id')
        def save(current):
            iam.require_space_capability(store,current,'resources')
            if project_id:
                iam.project_in_space(store,current,project_id)
            table,column,identifier=('project_execution_settings','project_id',project_id) if project_id else ('space_execution_settings','space_id',current.space_id)
            row=store.one('SELECT * FROM '+table+' WHERE '+column+'=?',(identifier,))
            checked_version(row,body.expected_version)
            store.db.execute('INSERT INTO '+table+'('+column+',settings) VALUES(?,?) ON CONFLICT('+column+') DO UPDATE SET settings=excluded.settings,version='+table+'.version+1',
                (identifier,body.settings.model_dump_json()))
            resources.changed(current,identifier)
            row=store.one('SELECT * FROM '+table+' WHERE '+column+'=?',(identifier,))
            return {**row,'settings':json.loads(row['settings'])}
        return mutate(store,principal,'execution.settings:'+(project_id or 'workspace'),body.idempotency_key,body.model_dump(),save)

    @router.post('/api/operations/{operation_id}/release-source')
    @database_endpoint(store)
    def release_source(operation_id: str,request: Request,body: ReleaseHold):
        principal=iam.require_space_capability(store,auth.panel(request,True),'resources')
        with store.transaction():
            row=store.one('SELECT * FROM operations WHERE id=? AND space_id=?',(operation_id,principal.space_id))
            if not row or row['state']!=body.expected_state or not row.get('route_context'):
                raise DevError('SOURCE_HOLD_CHANGED','This operation no longer has the expected uncertain source hold',409)
            store.db.execute('UPDATE operations SET source_lock_released=1 WHERE id=?',(operation_id,))
            iam.audit(store,principal,'execution.source_hold_released',operation_id,
                      detail={'remote_stopped_confirmation':True,'process_killed':False})
        runtime.wake_delivery()
        return {'operation_id':operation_id,'source_hold_released':True,'process_killed':False}
    return router
