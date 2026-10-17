"""Projects have exactly one workspace-provided folder, with no device/root fallback."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

from fastapi import APIRouter, Request, Query

from hub import iam
from hub.api.context import HubContext
from hub.db_worker import database_endpoint
from hub.permissions import require_access
from hub.workspace_api import ProjectSave
from hub.workspace_resources import checked_version
from shared.util import DevError


def make_projects_router(context: HubContext):
    router=APIRouter()
    auth,store,runtime=context.auth,context.store,context.runtime

    @router.get('/api/projects')
    @router.get('/api/spaces/{space_id}/projects')
    @database_endpoint(store)
    def projects(request: Request):
        principal=auth.panel(request)
        if principal.space_id:
            return {'projects':runtime.list_projects(principal)}
        from hub.iam_api import spaces_for
        result=[]
        for space in spaces_for(store,principal.user_id):
            scoped=iam.live_principal(store,replace(principal,space_id=space['id']))
            result.extend({**project,'workspace':{'id':space['id'],'label':space['label']}}
                          for project in runtime.list_projects(scoped))
        return {'projects':result}

    @router.get('/api/projects/{project_id}')
    @database_endpoint(store)
    def project(project_id: str,request: Request):
        principal=auth.panel(request)
        row=runtime.project(project_id,principal)
        return runtime.project_public(row)

    async def create_for_connection(arguments,principal):
        body=ProjectSave.model_validate(arguments)
        result=await runtime.workspace_resources.validated_save('project',principal,body)
        fresh=await store.run(iam.live_principal,store,principal)
        try:
            await store.run(require_access,store,fresh,'read',project_id=result['id'])
            result['accessible']=True
        except DevError:
            result['accessible']=False
        return {k:result[k] for k in ('id','alias','description','version','accessible')}
    runtime.project_creator=create_for_connection

    @router.post('/api/spaces/{space_id}/projects',status_code=201)
    async def create_project(request: Request,body: ProjectSave):
        principal=await store.run(auth.panel,request,True)
        return await runtime.workspace_resources.validated_save('project',principal,body,reauthenticate=lambda:auth.panel(request,True))

    @router.put('/api/projects/{project_id}')
    async def update_project(project_id: str,request: Request,body: ProjectSave):
        principal=await store.run(auth.panel,request,True)
        return await runtime.workspace_resources.validated_save('project',principal,body,project_id,reauthenticate=lambda:auth.panel(request,True))

    @router.delete('/api/projects/{project_id}')
    @database_endpoint(store)
    def remove_project(project_id: str,request: Request,expected_version: int=Query(ge=1)):
        with store.transaction():
            principal=iam.require_space_capability(store,auth.panel(request,True),'resources')
            row=iam.project_in_space(store,principal,project_id)
            checked_version(row,expected_version)
            if store.one("SELECT id FROM operations WHERE project_id=? AND state IN ('queued','running','reconnecting','cancelling','needs_review') LIMIT 1",(project_id,)):
                raise DevError('PROJECT_HAS_ACTIVE_OPERATIONS','Resolve or cancel the original operations before removing this project',409)
            if store.one("SELECT v.id FROM space_vps v JOIN vps_projects vp ON vp.vps_id=v.id WHERE vp.project_id=? AND v.display_mode IN ('project_only','both') AND (SELECT count(*) FROM vps_projects other WHERE other.vps_id=v.id)=1",(project_id,)):
                raise DevError('VPS_DISPLAY_REQUIRES_PROJECT','Change VPS display settings before removing their last project entry',409)
            try:
                store.db.execute('DELETE FROM projects WHERE id=?',(project_id,))
            except sqlite3.IntegrityError as exc:
                raise DevError('PROJECT_HAS_RECORDS','The project still has retained records or resource references; it was not deleted',409) from exc
            iam.audit(store,principal,'project.removed',project_id,detail={'files_deleted':False})
            runtime.workspace_resources.changed(principal,project_id)
        return {'id':project_id,'files_deleted':False}
    return router
