"""Pool/Folder peers, inherited defaults and independent Device authority."""
import uuid
from dataclasses import replace

import pytest

from hub.permissions import require_access
from hub.runtime import Runtime
from hub.workspace_api import PoolCreate, PoolSave, FolderSave, SourceSave, ProjectSave, DeviceBindingSave
from hub.workspace_sources import source_views, select_view, resolve_shell
from hub.vps import VPSBindingSave, VPSInput
from shared.util import DevError
from tests.workspace_fixtures import WorkspaceFixture


@pytest.fixture
def resources(tmp_path):
    fixture=WorkspaceFixture(tmp_path/'hub')
    runtime=Runtime(fixture.store)
    calls=[]
    async def validate(tool,args,project,principal):
        assert tool=='system_validate'
        device=runtime.workspace_resources.require_validation(principal,project,tool)
        calls.append((device,project['root']))
        return {'root':project['root'],'writable':True,'allow_tasks':True}
    runtime.dispatch=validate
    fixture.store.execute('DELETE FROM space_devices')
    yield fixture,runtime.workspace_resources,calls
    fixture.close()


def source_body(**changes):
    return SourceSave.model_validate({'name':'Imported pool','source_kind':'storage','pool_id':'pool',
        'mapping_ids':['map_a','map_b'],'subpath':'team','mode':'write','allow_tasks':True,
        'idempotency_key':uuid.uuid4().hex,**changes})


def pseudo(source,space='alice',subpath='codepier'):
    return {'id':'','space_id':space,'source_id':source['id'],'subpath':subpath}


@pytest.mark.asyncio
async def test_pool_import_and_execution_without_independent_device_binding(resources):
    ws,service,calls=resources
    body=source_body()
    saved=await service.validated_save('source',ws.principal,body)
    assert set(calls)=={('a','/mnt/a/team'),('b','/mnt/b/team')}
    project=pseudo(saved)
    assert select_view(ws.store,project,'files')['device_id']=='b'
    assert select_view(ws.store,project,'shell')['device_id']=='a'
    assert resolve_shell(ws.store,project)['allowed_device_ids']=={'a','b'}
    assert not ws.store.all('SELECT * FROM space_devices')
    with pytest.raises(DevError,match='cannot perform'):
        require_access(ws.store,ws.principal,'devices.read',device_id='a')
    again=await service.validated_save('source',ws.principal,body)
    assert again['id']==saved['id'] and again['replayed']
    assert len(calls)==2
    # Changing the Pool defaults updates inherited sources rather than copied values.
    old=ws.store.one("SELECT * FROM storage_pools WHERE id='pool'")
    service.save_pool(ws.principal,PoolSave(name=old['name'],abstract_base=old['abstract_base'],
        default_fs_device_id='a',default_shell_device_id='b',expected_version=old['version'],
        idempotency_key=uuid.uuid4().hex),'pool')
    assert select_view(ws.store,project,'files')['device_id']=='a'
    assert select_view(ws.store,project,'shell')['device_id']=='b'


@pytest.mark.asyncio
async def test_default_excluded_requires_explicit_override_and_no_extra_mapping(resources):
    ws,service,_=resources
    with pytest.raises(DevError):
        await service.validated_save('source',ws.principal,source_body(mapping_ids=['map_a','map_c']))
    saved=await service.validated_save('source',ws.principal,source_body(mapping_ids=['map_a','map_c'],
        files={'mode':'custom','device_id':'a'},shell={'mode':'custom','default_device_id':'c'}))
    project=pseudo(saved)
    assert set(source_views(ws.store,project)['views'])=={'a','c'}
    assert select_view(ws.store,project,'shell')['device_id']=='c'
    assert select_view(ws.store,project,'files')['device_id']=='a'
    with pytest.raises(DevError):
        select_view(ws.store,project,'shell','b')
    with pytest.raises(DevError):
        source_views(ws.store,{**project,'space_id':'bob'})


@pytest.mark.asyncio
async def test_project_can_use_pool_root_without_creating_folder(resources):
    ws,service,_=resources
    source=await service.validated_save('source',ws.principal,source_body(subpath='.'))
    project=await service.validated_save('project',ws.principal,ProjectSave(alias='root-project',source_id=source['id'],
        idempotency_key=uuid.uuid4().hex))
    assert project['subpath']=='.'
    assert select_view(ws.store,project)['root']=='/mnt/b'
    assert not ws.store.all('SELECT * FROM folders')


@pytest.mark.asyncio
async def test_direct_folder_is_peer_and_only_provides_its_device(resources):
    ws,service,_=resources
    folder=await service.validated_save('folder',ws.principal,FolderSave(name='Direct',device_id='a',device_path='/mnt/direct',
        mode='write',allow_tasks=True,idempotency_key=uuid.uuid4().hex))
    source=await service.validated_save('source',ws.principal,SourceSave(name='Direct import',source_kind='folder',
        folder_resource_id=folder['id'],subpath='team',mode='write',allow_tasks=True,idempotency_key=uuid.uuid4().hex))
    project=pseudo(source)
    assert set(source_views(ws.store,project)['views'])=={'a'}
    assert select_view(ws.store,project)['root']=='/mnt/direct/team/codepier'
    with pytest.raises(DevError):
        select_view(ws.store,project,'shell','b')
    assert not ws.store.all('SELECT * FROM space_devices')


@pytest.mark.asyncio
async def test_pool_defaults_validated_atomically_and_required_at_creation(resources):
    ws,service,calls=resources
    pool=await service.validated_save('pool',ws.principal,PoolCreate(name='Development',abstract_base='/storage/development',
        default_fs_device_id='a',default_shell_device_id='b',confirmed_same_storage=True,
        mappings=[{'device_id':d,'path':'/srv/'+d} for d in ('a','b')],idempotency_key=uuid.uuid4().hex))
    assert len(calls)==2 and pool['default_shell_device_id']=='b'
    assert len(ws.store.all('SELECT * FROM storage_mappings WHERE pool_id=?',(pool['id'],)))==2
    previous=service.runtime.dispatch
    async def readonly(*args):
        result=await previous(*args)
        return {**result,'writable':False,'allow_tasks':False}
    service.runtime.dispatch=readonly
    with pytest.raises(DevError,match='shell default'):
        await service.validated_save('pool',ws.principal,PoolCreate(name='Read only',abstract_base='/storage/readonly',
            default_fs_device_id='a',default_shell_device_id='a',confirmed_same_storage=True,
            mappings=[{'device_id':'a','path':'/readonly'}],idempotency_key=uuid.uuid4().hex))
    assert not ws.store.one("SELECT id FROM storage_pools WHERE name='Read only'")
    assert not ws.store.one("SELECT id FROM storage_mappings WHERE path='/readonly'")


@pytest.mark.asyncio
async def test_pool_default_edit_rolls_back_if_an_import_excludes_it(resources):
    ws,service,_=resources
    await service.validated_save('source',ws.principal,source_body())
    with pytest.raises(DevError,match='invalidate'):
        service.save_pool(ws.principal,PoolSave(name='Shared',abstract_base='/storage/shared',default_fs_device_id='b',
            default_shell_device_id='c',expected_version=1,idempotency_key=uuid.uuid4().hex),'pool')
    pool=ws.store.one("SELECT * FROM storage_pools WHERE id='pool'")
    assert pool['version']==1 and pool['default_shell_device_id']=='a'


@pytest.mark.asyncio
async def test_source_snapshot_and_version_checked_after_directory_validation(resources):
    ws,service,_=resources
    previous=service.runtime.dispatch
    async def changed(*args):
        result=await previous(*args)
        ws.store.execute("UPDATE storage_mappings SET path='/changed' WHERE id='map_b'")
        return result
    service.runtime.dispatch=changed
    with pytest.raises(DevError,match='changed'):
        await service.validated_save('source',ws.principal,source_body())
    assert not ws.store.one("SELECT id FROM space_sources WHERE name='Imported pool'")
    assert not ws.store.one("SELECT receipt FROM workspace_requests WHERE operation='source.save:new'")


def test_independent_device_binding_disable_does_not_revoke_source(resources):
    ws,service,_=resources
    service.bind_device(ws.principal,'a',DeviceBindingSave(idempotency_key=uuid.uuid4().hex))
    service.bind_device(ws.principal,'a',DeviceBindingSave(enabled=False,expected_version=1,idempotency_key=uuid.uuid4().hex))
    assert select_view(ws.store,ws.project,'shell')['device_id']=='a'
    with pytest.raises(DevError):
        require_access(ws.store,ws.principal,'devices.read',device_id='a')


def test_files_and_shell_default_availability_are_independent(resources):
    ws,_,_=resources
    ws.store.execute("UPDATE devices SET enabled=0 WHERE id='a'")
    assert select_view(ws.store,ws.project,'files')['device_id']=='b'
    with pytest.raises(DevError,match='no other device'):
        select_view(ws.store,ws.project,'shell')
    ws.store.execute("UPDATE devices SET enabled=1 WHERE id='a'")
    ws.store.execute("UPDATE devices SET enabled=0 WHERE id='b'")
    assert select_view(ws.store,ws.project,'shell')['device_id']=='a'
    with pytest.raises(DevError,match='no fallback'):
        select_view(ws.store,ws.project,'files')


@pytest.mark.asyncio
async def test_pool_shell_allowlist_and_space_override_cannot_expand_each_other(resources):
    ws,service,_=resources
    ws.store.execute("UPDATE storage_pools SET shell_allowed_device_ids='[\"a\"]'")
    with pytest.raises(DevError):
        await service.validated_save('source',ws.principal,source_body(shell={'mode':'custom','default_device_id':'b'}))
    source=await service.validated_save('source',ws.principal,source_body())
    assert resolve_shell(ws.store,pseudo(source))['allowed_device_ids']=={'a'}


def test_vps_launcher_is_a_technical_dependency_not_a_device_grant(resources):
    ws,service,_=resources
    vps=service.runtime.vps
    inventory=vps.save(VPSInput(name='Server',idempotency_key=uuid.uuid4().hex),ws.principal)
    bound=vps.bind(VPSBindingSave(resource_id=inventory['id'],name='Server',runner_device_id='a',
        connection_ref='production',idempotency_key=uuid.uuid4().hex),ws.principal)
    result=vps.execution_project({'target':'vps:'+bound['id']},ws.principal)
    assert result['device_id']=='a' and not ws.store.all('SELECT * FROM space_devices')
    with pytest.raises(DevError):
        require_access(ws.store,ws.principal,'devices.read',device_id='a')
    with pytest.raises(DevError):
        vps.execution_project({'target':'vps:'+bound['id']},replace(ws.principal,space_id='bob'))


@pytest.mark.asyncio
async def test_pool_create_rejects_stale_version_before_contacting_runner(resources):
    ws,service,calls=resources
    with pytest.raises(DevError,match='Configuration changed'):
        await service.validated_save('pool',ws.principal,PoolCreate(name='Stale creation',abstract_base='/storage/stale',
            default_fs_device_id='a',default_shell_device_id='a',expected_version=1,confirmed_same_storage=True,
            mappings=[{'device_id':'a','path':'/mnt/a'}],idempotency_key=uuid.uuid4().hex))
    assert calls==[] and not ws.store.one("SELECT id FROM storage_pools WHERE name='Stale creation'")


@pytest.mark.parametrize('dependency',['folder','pool','selected_mapping'])
def test_live_inventory_edits_invalidate_panel_source_views(resources,dependency,tmp_path):
    from hub import iam
    from tests.current_model import CurrentModel
    ws,_,_=resources
    if dependency=='folder':
        CurrentModel(ws.store).folder('direct-source',space='alice',device='a',path=tmp_path)
    before=iam.panel_access_snapshot(ws.store,ws.principal)
    if dependency=='folder':
        ws.store.execute("UPDATE folders SET device_path=device_path||'/changed',version=version+1 WHERE id='direct-source'")
    elif dependency=='pool':
        ws.store.execute("UPDATE storage_pools SET default_shell_device_id='b',version=version+1 WHERE id='pool'")
    else:
        ws.store.execute("UPDATE storage_mappings SET path='/changed',version=version+1 WHERE id='map_a'")
    # Event streams use this relation to revalidate before exposing any frame.
    assert not before <= iam.panel_access_snapshot(ws.store,ws.principal)


def test_unprovided_mapping_edits_do_not_invalidate_or_expand_workspace(resources):
    from hub import iam
    ws,_,_=resources
    before=iam.panel_access_snapshot(ws.store,ws.principal)
    ws.store.execute("UPDATE storage_mappings SET path='/unprovided',version=version+1 WHERE id='map_c'")
    assert iam.panel_access_snapshot(ws.store,ws.principal)==before
    assert set(source_views(ws.store,ws.project)['views'])=={'a','b'}
