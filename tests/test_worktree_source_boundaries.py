"""Real managed worktrees retain both source and exact derived-root authority.

Only disposable Git repositories, local filesystem state and synthetic connection
identities are used. No live project, SSH target or user credential is accessed.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import uuid

import pytest

from agent.runner import Agent
from agent.routed_execution import source_boundary
from shared.util import DevError, atomic_json


@pytest.fixture
def worktree_agent(tmp_path):
    base=(tmp_path/'allowed').resolve();source=base/'source';source.mkdir(parents=True)
    (source/'tracked.txt').write_text('committed source\n')
    def git(*args):
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@invalid',
            '-c','core.hooksPath='+str(tmp_path/'no-hooks'),*args],cwd=source,
            capture_output=True,check=True)
    git('init');git('add','tracked.txt');git('commit','-m','Isolated test source')
    config=tmp_path/'runtime'/'config.json'
    atomic_json(config,{'hub_url':'127.0.0.1:9','device_id':'fixture-runner','secret':'x'*43,
        'state_dir':str(tmp_path/'state'),
        'allowed_roots':[{'path':str(base),'writable':True,'allow_tasks':True}],
        'shell':{'enabled':True,'projects':['*']},'tasks':{}})
    agent=Agent(config)
    project={'id':'fixture-project','alias':'Fixture','device_id':'fixture-runner',
        'root':str(source),'mode':'write','allow_tasks':True,
        '_integration_owner':'connection:first','_coding_device':'fixture-runner',
        '_route_context':{'source_key':'device:fixture-runner','family':'git'},
        '_source_boundary':{'mount':str(base),'subpath':'source','protocol':1}}
    try:yield agent,project
    finally:
        agent.journal.db.close()
        agent.instance_lock.close()


async def create(agent,project):
    identifier=uuid.uuid4().hex
    result=await agent.integrations.workspaces.create(identifier,project,
        {'project':'Fixture','base_ref':'HEAD','label':'Boundary regression','workspace_id':''})
    assert result['state']=='ready'
    return result


@pytest.mark.asyncio
async def test_created_sibling_worktree_has_exact_boundary_and_restorable_source(worktree_agent):
    agent,project=worktree_agent;manager=agent.integrations.workspaces
    result=await create(agent,project);path=Path(result['path'])
    assert not path.is_relative_to(project['root'])
    view=manager.resolve(project,result['workspace_id'])
    assert view['_source_boundary']=={'mount':str(path),'subpath':'.','protocol':1}
    assert view['_original_source_boundary']==project['_source_boundary']
    assert view['_route_context']==project['_route_context']
    assert view['_integration_owner']==project['_integration_owner']
    source_boundary(view)
    parent=manager.parent_project(view)
    source_boundary(parent)
    assert parent['root']==project['root'] and parent['_source_boundary']==project['_source_boundary']
    assert '_original_source_boundary' not in parent
    assert manager.resolve(view,result['workspace_id'])['root']==str(path)
    assert (path/'tracked.txt').read_text()=='committed source\n'
    assert agent.engine.root(view,True)[0]==path
    removed=await manager.remove(uuid.uuid4().hex,project,{
        'target_workspace_id':result['workspace_id'],'confirm':result['workspace_id'],'workspace_id':''})
    assert removed['removed'] and not path.exists()
    assert (Path(project['root'])/'tracked.txt').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['parent','sibling','source'])
async def test_derived_boundary_never_grants_parent_or_other_directory(worktree_agent,change):
    agent,project=worktree_agent;result=await create(agent,project)
    view=agent.integrations.workspaces.resolve(project,result['workspace_id'])
    root=Path(view['root'])
    targets={'parent':root.parent,'sibling':root.parent/'another','source':Path(project['root'])}
    with pytest.raises(DevError) as failure:
        agent.engine.root({**view,'root':str(targets[change])})
    assert failure.value.code=='SOURCE_BOUNDARY_INVALID'


@pytest.mark.asyncio
async def test_owner_scope_and_local_readonly_limits_are_not_bypassed(worktree_agent):
    agent,project=worktree_agent;result=await create(agent,project);manager=agent.integrations.workspaces
    with pytest.raises(DevError) as failure:
        manager.resolve({**project,'_integration_owner':'connection:second'},result['workspace_id'])
    assert failure.value.code=='RECORD_NOT_FOUND'
    view=manager.resolve(project,result['workspace_id'])
    agent.config['allowed_roots'].append({'path':view['root'],'writable':False,'allow_tasks':False})
    with pytest.raises(DevError) as failure:agent.engine.root(view,True)
    assert failure.value.code=='READ_ONLY'
    assert agent.engine.root(manager.parent_project(view),True)[0]==Path(project['root'])


@pytest.mark.asyncio
async def test_original_source_move_is_checked_before_recorded_worktree_access(worktree_agent):
    agent,project=worktree_agent;result=await create(agent,project)
    source=Path(project['root']);source.rename(source.with_name('moved-source'))
    with pytest.raises(DevError) as failure:
        agent.integrations.workspaces.resolve(project,result['workspace_id'])
    assert failure.value.code=='SOURCE_BOUNDARY_INVALID'
    assert Path(result['path']).is_dir()
