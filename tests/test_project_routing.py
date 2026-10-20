"""Real shared-folder files, independent execution and original RCH recovery.

Only workspace resource bindings exist; SourceRoot/Role/action-route objects and
adoption/fallback behavior have no positive compatibility tests.
"""
from __future__ import annotations

from contextlib import closing
import json
import sys
import uuid

from hub.store import Store
from shared.util import atomic_json
from tests.routing_support import routing_stack  # noqa: F401


def storage_settings(r, **changes):
    return {'pool_id':r.pool['id'],'name':r.binding['name'],
        'mapping_ids':[m['id'] for m in r.mappings.values()],
        'default_device_id':r.s.device,'files_device_id':r.builtin,'allowed_device_ids':[],
        'expected_version':r.binding['version'],'idempotency_key':uuid.uuid4().hex,**changes}


def test_shared_project_files_git_rch_and_safe_disable(routing_stack):
    r=routing_stack
    p=next(p for p in r.get('/api/projects')['projects'] if p['id']==r.project['id'])
    assert p['folder_id']==r.folder['id'] and p['root']=='/storage/shared-test/Imago'
    with closing(Store(r.s.hubdir)) as store:
        logical=store.one('SELECT * FROM projects WHERE id=?',(p['id'],))
        assert not {'device_id','root','routing_managed','source_root_id'} & logical.keys()
    options=r.s.call('get_execution_options',{'project':p['id'],'action':'git'})
    assert options['default_device_id']==r.s.device
    written=r.call('write',{'path':'new.txt','content':'uncommitted-shared-bytes',
        'expected_sha256':'new','idempotency_key':uuid.uuid4().hex})
    if written.get('id'):assert written['device_id']==r.builtin
    assert written.get('sha256') or written.get('state')=='succeeded',written
    git=r.exec(['git','status','--porcelain'],action='git')
    assert git['device_id']==r.s.device and git['state']=='succeeded',git
    assert 'new.txt' in git['result']['data']['output']
    key=uuid.uuid4().hex
    argv=[sys.executable,'-c','from pathlib import Path; assert Path("new.txt").read_text()=="uncommitted-shared-bytes"; print("RCH fixture verified")']
    outcome=r.exec(argv,key=key)
    assert outcome['state']=='succeeded' and outcome['device_id']==r.s.device,outcome
    assert outcome['result']['data']['provider']=='rch'
    assert outcome['result']['data']['rch']['terminal_acknowledged'] is True
    assert r.exec(argv,key=key)['id']==outcome['id']
    assert r.fake.with_suffix('.count').read_text().splitlines()==['exec']
    for values,project in zip(r.project_requests,r.projects):
        assert r.post(r.space_path+'/projects',values)['id']==project['id']
    # Disabling a workspace source does not delete its Project, global pool or
    # physical bytes. There is no detached legacy device/root fallback.
    r.put(r.space_path+'/storage/'+r.binding['id'],storage_settings(r,enabled=False))
    after=next(p for p in r.get('/api/projects')['projects'] if p['id']==r.project['id'])
    assert after['folder_id']==r.folder['id']
    assert after['source_bound'] is False and not after.get('device_id')
    assert after['source_error']=='STORAGE_BINDING_UNAVAILABLE'
    assert (r.s.imago/'new.txt').read_text()=='uncommitted-shared-bytes'
    assert any(p['id']==r.pool['id'] for p in r.get('/api/storage-pools')['pools'])
    denied=r.s.call('read',{'project':p['id'],'path':'new.txt'},raw=True)
    assert denied.status_code==409 and denied.json()['error']['code']=='STORAGE_BINDING_UNAVAILABLE'


def test_space_recipes_hooks_and_unknown_rch_recovery(routing_stack):
    r=routing_stack
    r.put(r.space_path+'/execution',{'expected_version':r.execution['version'],
        'idempotency_key':uuid.uuid4().hex,'settings':{
            'hooks':[{'name':'remote-tests','actions':['test'],'handler':'require_rch'}],
            'tests':[{'name':'unit','argv':[sys.executable,'-c','print("configured space test")']}]
        }})
    result=r.exec(task='unit')
    assert result['state']=='succeeded',result
    assert 'configured space test' in result['result']['data']['output']
    uncertain=r.exec([sys.executable,'-c','import time; time.sleep(20)'],timeout_seconds=1)
    assert uncertain['state']=='needs_review',uncertain
    control=r.call('routed_job_control',{'original_operation_id':uncertain['id'],'action':'recover',
        'timeout_seconds':3,'idempotency_key':uuid.uuid4().hex})
    assert control['state']=='succeeded',control
    assert control['result']['data']['command_replayed'] is False
    assert control['result']['data']['rch']['terminal_acknowledged']
    assert r.fake.with_suffix('.count').read_text().splitlines()==['exec','exec']
    assert r.get('/api/operations/'+uncertain['id'])['source_lock_released']==1
    r.put('/api/projects/'+r.project['id']+'/execution',{'expected_version':0,
        'idempotency_key':uuid.uuid4().hex,'settings':{'hooks':[
            {'name':'stop','actions':['test'],'handler':'deny','reason':'fixture policy'}]}})
    denied=r.s.call('exec',{'project':r.project['id'],'action':'test','argv':['echo','no'],
        'idempotency_key':uuid.uuid4().hex},raw=True)
    assert denied.status_code==403 and denied.json()['error']['code']=='EXECUTION_HOOK_DENIED'


def test_subfolder_traversal_symlink_and_disabled_project_shell(routing_stack):
    r=routing_stack
    r.s.nexus.joinpath('secret.txt').write_text('sibling')
    path=r.space_path+'/projects'
    invalid={'alias':'escape','folder_id':r.folder['id'],'subpath':'../outside','idempotency_key':uuid.uuid4().hex}
    assert r.client.post(path,json=invalid).status_code==422
    r.s.root.joinpath('link').symlink_to(r.s.nexus,target_is_directory=True)
    invalid.update(alias='link',subpath='link',idempotency_key=uuid.uuid4().hex)
    response=r.client.post(path,json=invalid)
    if response.status_code==409 and response.json()['error']['code']=='VALIDATION_PENDING':
        op=r.s.poll(response.json()['error']['operation_id'],timeout=30)
        assert op['state']=='failed',op
        response=r.client.post(path,json=invalid)
    assert response.status_code==409,response.text
    assert not any(p['alias'] in {'escape','link'} for p in r.get('/api/projects')['projects'])
    r.put('/api/projects/'+r.project['id']+'/shell',{'expected_version':0,
        'idempotency_key':uuid.uuid4().hex,'settings':{'mode':'disabled'}})
    denied=r.s.call('exec',{'project':r.project['id'],'action':'git','argv':['git','status'],
        'idempotency_key':uuid.uuid4().hex},raw=True)
    assert denied.status_code==403 and denied.json()['error']['code']=='SHELL_DISABLED'


def test_fixed_connection_source_snapshot_never_follows_a_rebound_folder(routing_stack):
    r=routing_stack
    profile=r.s.create_profile({'project_rules':[{'projects':[r.project['id']],
        'actions':['read','write','execute']}]})
    connection=r.s.create_connection(profile)
    result=r.s.mcp('get_execution_options',{'project':r.project['id']},token_value=connection['token'])
    assert not result.get('isError'),result
    assert result['structuredContent']['default_device_id']==r.s.device
    # Deliberately rebind the same Project into another existing subdirectory
    # using reviewed source validation. A fixed Connection cannot follow it.
    r.put('/api/projects/'+r.project['id'],{'alias':r.project['alias'],'folder_id':r.folder['id'],
        'subpath':'Nexus','expected_version':r.project['version'],'idempotency_key':uuid.uuid4().hex})
    denied=r.s.mcp('exec',{'project':r.project['id'],'action':'test','argv':['echo','denied'],
        'idempotency_key':uuid.uuid4().hex},token_value=connection['token'])
    assert denied.get('isError') and 'CONNECTION_TARGET_CHANGED' in json.dumps(denied),denied
    assert not r.fake.with_suffix('.count').exists()


def test_missing_local_rch_has_no_fallback(routing_stack):
    r=routing_stack
    r.s.stop_agent();r.s.config['rch']['enabled']=False
    atomic_json(r.s.config_path,r.s.config);r.s.start_agent()
    result=r.exec([sys.executable,'-c','from pathlib import Path; Path("should-not-exist").write_text("bad")'])
    assert result['state']=='failed',result
    assert result['result']['error']['code']=='RCH_LOCAL_DISABLED'
    assert not r.s.imago.joinpath('should-not-exist').exists()
