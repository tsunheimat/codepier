"""Independent regressions for native replay ordering and offline history safety."""
from contextlib import closing
import time
import uuid
import base64

import pytest
from hub.runtime import Runtime
from hub.store import Store
from tests.current_model import CurrentModel
from shared.native_cli import database


class NoAck:
    async def send(self, message):
        self.message = message


@pytest.fixture
def native_cache(tmp_path):
    store=Store(tmp_path/'hub');model=CurrentModel(store)
    model.user('u',username='admin');space=model.workspace('native-cache-space',owner='u')
    device=uuid.uuid4().hex;project_id=uuid.uuid4().hex
    model.device(device,owner='u');model.bind_device(space,device)
    root=tmp_path/'source';root.mkdir()
    folder=model.folder('native-cache-folder',space=space,device=device,path=root)
    model.project(project_id,space=space,folder=folder,owner='u',alias='Fixture')
    runtime=Runtime(store);service=runtime.native
    project=service.project(project_id,model.panel('u',space))
    row={'id':uuid.uuid4().hex,'project_id':project_id,'device_id':device,
        'root':str(root),'cwd':str(root),'provider':'codex','title':'Owned session',
        'status':'running','created':time.time()-50,'updated':time.time()-20,
        'size':5,'exit_code':None,'error':'','mode':'terminal','native_thread':'','chat_settings':'{}',
        '_native_owner':'user:u','_native_space':space}
    try:yield service,project,row
    finally:store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cleared', ['cleared', 'deleted'])
async def test_old_sync_never_resurrects_cleared_history(native_cache, cleared):
    service, project, old = native_cache
    con = NoAck()
    packet = {'type': 'native_sync', 'sessions': [old], 'chunks': [
        {'id': old['id'], 'offset': 0, 'data': base64.b64encode(b'hello').decode()}]}
    await service.receive(project['device_id'], con, packet)
    tombstone = {**old, 'status': cleared, 'size': 0, 'updated': time.time()}
    await service.receive(project['device_id'], con, {'type': 'native_sync', 'sessions': [tombstone], 'chunks': []})
    await service.receive(project['device_id'], con, packet)
    with closing(database(service.directory)) as db:
        row = db.execute('SELECT * FROM sessions WHERE id=?', (old['id'],)).fetchone()
        assert row['status'] == cleared
        assert db.execute('SELECT count(*) FROM output WHERE session=?', (old['id'],)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_late_start_reply_never_revives_exited_session(native_cache):
    service, project, row = native_cache
    con = NoAck()
    await service.receive(project['device_id'], con, {'type': 'native_sync', 'sessions': [
        {**row, 'status': 'exited', 'exit_code': 0, 'updated': time.time()}], 'chunks': []})
    await service.receive(project['device_id'], con, {'type': 'native_sync', 'sessions': [
        {**row, 'status': 'starting'}], 'chunks': []})
    with closing(database(service.directory)) as db:
        result = db.execute('SELECT status,exit_code FROM sessions WHERE id=?', (row['id'],)).fetchone()
        assert result['status'] == 'exited'
        assert result['exit_code'] == 0
