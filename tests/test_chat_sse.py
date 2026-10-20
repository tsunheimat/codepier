"""Current native replay, explicit metadata, live authority and real framing."""
import asyncio
import base64
from contextlib import closing
import json
from types import SimpleNamespace
import uuid

import pytest
from starlette.requests import Request
from hub.native_cli import make_native_router
from hub.store import Store
from hub.runtime import Runtime
from shared.native_cli import database, public
from shared.util import DevError
from tests.current_model import CurrentModel


@pytest.fixture
def service(tmp_path):
    store=Store(tmp_path/'hub');model=CurrentModel(store)
    model.user('owner');model.workspace('native-space',owner='owner')
    model.device('device',owner='owner');model.bind_device('native-space','device')
    source=tmp_path/'source';source.mkdir()
    model.folder('native-folder',space='native-space',device='device',path=source)
    model.project('project',space='native-space',folder='native-folder',owner='owner',alias='Fixture')
    runtime=Runtime(store);runtime.online=lambda _:True
    obj=runtime.native;project=obj.project('project',model.panel('owner','native-space'))
    sid=uuid.uuid4().hex
    with closing(database(obj.directory)) as db,db:
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (sid,'project','device',str(source),str(source),'pi','Chat','running',1,1,'chat'))
    store.execute('INSERT INTO native_ownership(id,space_id,owner_user_id,project_id,device_id,created) VALUES(?,?,?,?,?,?)',
        (sid,'native-space','owner','project','device',1))
    try:yield obj,project,sid
    finally:store.close()


def record(obj,sid):
    with closing(database(obj.directory)) as db:
        row=public(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
    return {**row,'_native_owner':'user:owner','_native_space':'native-space'}


async def sync(obj,sid,raw,offset=0):
    sent=[]
    async def send(data):sent.append(data)
    await obj.receive('device',SimpleNamespace(send=send),{'type':'native_sync','sessions':[record(obj,sid)],
        'chunks':[{'id':sid,'offset':offset,'data':base64.b64encode(raw).decode()}]})
    return sent[-1]['offsets'][sid]


@pytest.mark.asyncio
async def test_split_utf8_json_lines_replay_and_duplicate_ack(service):
    obj,_,sid=service
    raw=(json.dumps({'type':'text_delta','text':'你好'},ensure_ascii=False)+'\n').encode()
    split=raw.index('你'.encode())+1
    assert await sync(obj,sid,raw[:split])==split
    assert obj.event_batch(sid,0)[1]==[]
    assert await sync(obj,sid,raw[split:],split)==len(raw)
    assert obj.event_batch(sid,0)[1]==[(len(raw),{'type':'text_delta','text':'你好'})]
    assert await sync(obj,sid,raw[split:],split)==len(raw)
    assert obj.event_batch(sid,len(raw))[1]==[]
    with pytest.raises(DevError,match='边界'):obj.event_batch(sid,split)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing',['mode','_native_owner','_native_space'])
async def test_sync_missing_current_metadata_creates_neither_session_nor_ownership(service,missing):
    obj,_,sid=service;row=record(obj,sid)
    async def send(data):pass
    await obj.receive('device',SimpleNamespace(send=send),{'sessions':[row],'chunks':[],'type':'native_sync'})
    assert obj.revision==0
    candidate={**row,'id':uuid.uuid4().hex};candidate.pop(missing)
    await obj.receive('device',SimpleNamespace(send=send),{'sessions':[candidate],'chunks':[],'type':'native_sync'})
    with closing(database(obj.directory)) as db:
        assert not db.execute('SELECT id FROM sessions WHERE id=?',(candidate['id'],)).fetchone()
    assert not obj.runtime.store.one('SELECT id FROM native_ownership WHERE id=?',(candidate['id'],))
    assert obj.revision==0


@pytest.mark.asyncio
@pytest.mark.parametrize('version',[None,1,2,4,True])
async def test_old_or_unsupported_chat_protocol_has_no_fallback(service,version):
    obj,project,_=service
    con=SimpleNamespace(native_protocol=1,native_security_protocol=1,
        workspace_protocol=1,project_routing_protocol=1,native_chat_protocol=version)
    obj.runtime.connections['device']=con
    for action,args in [('start',{'mode':'chat'}),('chat_prompt',{}),('chat_interrupt',{}),('chat_answer',{})]:
        with pytest.raises(DevError) as denied:await obj.request(action,project,args)
        assert denied.value.code=='CLI_CHAT_AGENT_UPDATE_REQUIRED'
    assert not obj.pending


class SessionTestAuth:
    def __init__(self,principal):self.principal=principal;self.allowed=True
    def panel(self,request,write=False):
        if not self.allowed:raise DevError('LOGIN_REQUIRED','revoked',401)
        if write and request.headers.get('x-rd-csrf')!='valid':raise DevError('CSRF_REJECTED','csrf',403)
        return self.principal


def native_auth(obj):
    return SessionTestAuth(CurrentModel(obj.runtime.store).panel('owner','native-space'))


def request(headers=(),body=b'{}'):
    async def receive():return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':'GET','path':'/','headers':list(headers),'query_string':b''},receive)


@pytest.mark.asyncio
async def test_stream_last_id_auth_revocation_and_action_csrf(service):
    obj,_,sid=service
    auth=SessionTestAuth(CurrentModel(obj.runtime.store).panel('owner','native-space'))
    first=b'{"type":"text_delta","text":"one"}\n';second=b'{"type":"text_delta","text":"two"}\n'
    await sync(obj,sid,first+second)
    routes=make_native_router(auth,obj.runtime).routes
    endpoint=next(r.endpoint for r in routes if r.path.endswith('/events'))
    req=request([(b'last-event-id',str(len(first)).encode())])
    async def connected():return False
    req.is_disconnected=connected
    response=await endpoint(sid,req,0)
    assert response.headers['x-accel-buffering']=='no'
    iterator=response.body_iterator
    try:
        assert 'event: session' in await anext(iterator)
        frame=await anext(iterator)
        assert 'two' in frame and 'one' not in frame
        assert 'id: '+str(len(first+second)) in frame
        auth.allowed=False
        assert 'LOGIN_REQUIRED' in await anext(iterator)
        with pytest.raises(StopAsyncIteration):await anext(iterator)
        with pytest.raises(DevError):await endpoint(sid,req,0)
        auth.allowed=True
        action=next(r.endpoint for r in routes if r.path.endswith('/{action}'))
        with pytest.raises(DevError,match='csrf'):await action('chat_prompt',request())
        obj.runtime.store.execute("UPDATE space_sources SET allow_tasks=0 WHERE id='native-folder'")
        with pytest.raises(DevError) as denied:await endpoint(sid,req,0)
        assert denied.value.code=='DEFAULT_DEVICE_UNAVAILABLE'
    finally:await iterator.aclose()
