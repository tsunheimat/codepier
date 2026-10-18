"""Global history: authorization before counts/search, stable ordering and paging."""
import json
from pathlib import Path
import uuid
from contextlib import closing
from types import SimpleNamespace
import pytest
from tests.test_chat_sse import service, native_auth, request
from hub.native_cli import make_native_router
from shared.native_cli import database
from shared.util import DevError
from tests.current_model import CurrentModel


def endpoint(obj):
    return next(r.endpoint for r in make_native_router(native_auth(obj),obj.runtime).routes if r.path=='/api/native/sessions')


def add(obj,project,**extra):
    row=dict(id=uuid.uuid4().hex,project_id=project['id'],device_id=project['device_id'],root=project['root'],cwd=project['root'],provider='pi',title='Conversation',status='exited',created=1,updated=1,mode='chat')
    row.update(extra)
    store=obj.runtime.store
    model=CurrentModel(store)
    space=project['space_id']
    if not store.one('SELECT id FROM projects WHERE id=?',(project['id'],)):
        folder='history-folder-'+project['id']
        model.folder(folder,space=space,device=project['device_id'],path=project['root'],allow_tasks=project['allow_tasks'])
        model.project(project['id'],space=space,folder=folder,owner='owner',alias=project['alias'])
    store.execute('INSERT INTO native_ownership(id,space_id,owner_user_id,project_id,device_id,created) VALUES(?,?,?,?,?,?)',
        (row['id'],space,'owner',row['project_id'],row['device_id'],1))
    with closing(database(obj.directory)) as db,db:
        db.execute('INSERT INTO sessions('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
    return row['id']


@pytest.mark.asyncio
async def test_all_projects_search_names_paths_node_and_permissions(service):
    obj,first,sid=service;store=obj.runtime.store;model=CurrentModel(store)
    store.execute("UPDATE projects SET alias='MCP 主项目',alias_key='mcp 主项目' WHERE id='project'")
    store.execute("UPDATE devices SET name='Studio' WHERE id='device'")
    second_root=Path(first['root'])/'two';second_root.mkdir()
    model.device('laptop',owner='owner',name='Laptop');model.bind_device(first['space_id'],'laptop')
    model.folder('second-folder',space=first['space_id'],device='laptop',path=second_root)
    model.project('second',space=first['space_id'],folder='second-folder',owner='owner',alias='Nexus')
    hidden_root=Path(first['root'])/'hidden';hidden_root.mkdir()
    model.folder('hidden-folder',space=first['space_id'],device='device',path=hidden_root,allow_tasks=False)
    model.project('hidden',space=first['space_id'],folder='hidden-folder',owner='owner',alias='HIDDEN_RECORD_CANARY')
    principal=model.panel('owner',first['space_id'])
    first=obj.runtime.project('project',principal)
    second=obj.runtime.project('second',principal)
    hidden=obj.runtime.project('hidden',principal)
    sid2=add(obj,second,title='Other work',updated=5)
    add(obj,hidden,title='HIDDEN_RECORD_CANARY hidden')
    add(obj,first,root='/old-mapping',title='HIDDEN_RECORD_CANARY stale')
    add(obj,first,status='deleted',title='HIDDEN_RECORD_CANARY deleted')
    calls=[];original=obj.runtime.project
    def count(value,principal):
        calls.append(value);return original(value,principal)
    obj.runtime.project=count
    read=endpoint(obj);result=await read(request(),mode='chat')
    assert result['total']==2 and [r['id'] for r in result['sessions']]==[sid2,sid]
    assert calls.count('project')==1
    for query,expected in [('Nexus',sid2),('laptop',sid2),('/two',sid2),('主项目',sid),('studio',sid)]:
        result=await read(request(),q=query,mode='chat')
        assert [r['id'] for r in result['sessions']]==[expected]
    assert (await read(request(),q='HIDDEN_RECORD_CANARY',mode='chat'))['total']==0
    store.execute("UPDATE space_folders SET allow_tasks=0 WHERE id='native-folder'")
    assert (await read(request(),mode='chat'))['total']==1


@pytest.mark.asyncio
async def test_recent_order_tie_break_and_final_page_recovery(service):
    obj,p,sid=service
    a=add(obj,p,id='a'*32,created=2,updated=100)
    b=add(obj,p,id='b'*32,created=2,updated=100)
    read=endpoint(obj)
    assert [r['id'] for r in (await read(request(),limit=2))['sessions']]==[b,a]
    page=await read(request(),offset=2,limit=2)
    assert page['offset']==2 and page['sessions'][0]['id']==sid
    with closing(database(obj.directory)) as db,db:
        db.execute("UPDATE sessions SET status='deleted' WHERE id=?",(sid,))
    page=await read(request(),offset=2,limit=2)
    assert page['offset']==0 and len(page['sessions'])==2


@pytest.mark.asyncio
async def test_archive_beyond_thousand_rows_is_reachable(service):
    obj,p,_=service
    with closing(database(obj.directory)) as db,db:
        db.executemany('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)',[(f'{i:032x}',p['id'],p['device_id'],p['root'],p['root'],'pi','Archive','exited',2,2,'chat') for i in range(1101)])
    with obj.runtime.store.transaction():
        obj.runtime.store.db.executemany('INSERT INTO native_ownership(id,space_id,owner_user_id,project_id,device_id,created) VALUES(?,?,?,?,?,?)',
            [(f'{i:032x}',p['space_id'],'owner',p['id'],p['device_id'],2) for i in range(1101)])
    page=await endpoint(obj)(request(),offset=1050,limit=50,mode='chat')
    assert page['offset']==1050 and page['total']==1102 and len(page['sessions'])==50
    assert 'cached_bytes' in page['sessions'][0]


@pytest.mark.asyncio
async def test_search_split_utf8_escaped_json_and_delta_text_not_settings(service):
    obj,p,sid=service
    raw=(json.dumps({'type':'user','text':'中文项目'},ensure_ascii=False)+'\n'+json.dumps({'type':'message','text':'恢复成功'},ensure_ascii=True)+'\n'+json.dumps({'type':'delta','text':'cross-'})+'\n'+json.dumps({'type':'delta','text':'boundary'})+'\n'+json.dumps({'type':'settings','text':'private-setting-canary'})+'\n').encode()
    split=raw.index('中'.encode())+1
    with closing(database(obj.directory)) as db,db:
        db.execute('INSERT INTO output VALUES (?,?,?)',(sid,0,raw[:split]))
        db.execute('INSERT INTO output VALUES (?,?,?)',(sid,split,raw[split:]))
    read=endpoint(obj)
    for q in ['中文','恢复成功','cross-boundary']:
        assert (await read(request(),q=q,mode='chat'))['total']==1
    assert (await read(request(),q='private-setting-canary',mode='chat'))['total']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('action,code',[('chat_catalog','CLI_CATALOG_TIMEOUT'),('chat_prompt','CLI_UNCERTAIN')])
async def test_catalog_disconnect_is_read_retry_but_prompt_remains_uncertain(service,action,code):
    obj,project,_=service
    async def disconnected(value):
        raise ConnectionError('fixture connection lost')
    obj.runtime.connections['device']=SimpleNamespace(native_protocol=1,native_security_protocol=1,native_chat_protocol=3,workspace_protocol=1,project_routing_protocol=1,send=disconnected)
    with pytest.raises(DevError) as error:
        await obj.request(action,project,{})
    assert error.value.code==code
    assert not obj.pending


@pytest.mark.asyncio
async def test_history_requires_admin_even_for_empty_results(service):
    obj,_,_=service
    auth=native_auth(obj);auth.allowed=False
    read=next(r.endpoint for r in make_native_router(auth,obj.runtime).routes if r.path=='/api/native/sessions')
    with pytest.raises(DevError):await read(request(),q='none')
