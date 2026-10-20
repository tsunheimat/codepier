"""Native protocol real Hub/Agent and real Chromium; only disposable fixture processes."""
import base64
from contextlib import closing
import hashlib
import json
import os
import sys
from pathlib import Path
import time
import uuid

import httpx
import pytest
from playwright.sync_api import sync_playwright,expect
from shared.native_cli import database
from shared.util import atomic_json
from tests.support import running_stack,wait_for
from tests.test_native_cli import FAKE


@pytest.fixture(scope='module')
def cli_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('native-stack')) as s:
        s.stop_agent()
        bindir=s.directory/'fake-bin';bindir.mkdir();home=s.directory/'private-home';home.mkdir()
        for cli in ('codex','pi'):
            executable=bindir/cli;executable.write_text(FAKE);executable.chmod(0o700)
        s.config['shell']={'enabled':True,'projects':['*'],'inherit_env':False,'env':{'PATH':str(bindir)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.defpath,'HOME':str(home)}}
        atomic_json(s.config_path,s.config);s.start_agent()
        yield s
        # Workers deliberately outlive Agent. Explicitly stop only this fixture's owned workers.
        with closing(database(s.directory/'agent-state'/'native-cli')) as db,db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT id FROM sessions WHERE status IN ('starting','running')").fetchall():
                db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',(uuid.uuid4().hex,row['id'],'stop','{}',time.time()))
        def stopped():
            with closing(database(s.directory/'agent-state'/'native-cli')) as db:
                return not db.execute("SELECT 1 FROM sessions WHERE status IN ('starting','running')").fetchone()
        wait_for(stopped,10)


def call(s,action,args=None,project=None):
    return s.client.post('/api/native/'+action,json={'project':project or s.project['id'],'args':args or {}})


def start(s):
    sid=uuid.uuid4().hex;r=call(s,'start',{'id':sid,'cli':'codex','cwd':'src','model':'model space;$(no-shell)','effort':'high'});s.must(r)
    wait_for(lambda:len(read(s,sid))>40)
    return sid


def read(s,sid):
    offset=0;chunks=[]
    while True:
        result=s.must(s.client.get('/api/native/sessions/'+sid+'/output',params={'offset':offset}))
        chunks.extend(base64.b64decode(c) for c in result['chunks'])
        if result['next']==offset:break
        offset=result['next']
    return b''.join(chunks)


def test_encrypted_transport_offline_agent_hub_reconnect_and_no_duplicates(cli_stack):
    s=cli_stack;sid=start(s)
    assert ('READY:'+str(s.imago/'src')).encode() in read(s,sid)
    before=read(s,sid)
    # Stop disposable fixture Agent, not production. Worker must continue with no transport.
    s.stop_agent();time.sleep(.7)
    cache=s.must(s.client.get('/api/native/sessions'))['sessions'];assert any(r['id']==sid for r in cache)
    with closing(database(s.directory/'agent-state'/'native-cli')) as db:
        worker_size=db.execute('SELECT size FROM sessions WHERE id=?',(sid,)).fetchone()[0]
    assert worker_size>len(before)
    s.start_agent();wait_for(lambda:len(read(s,sid))>worker_size)
    after=read(s,sid);assert after.startswith(before)
    # Hub process restart cannot terminate worker; durable cache and offsets survive.
    s.hub.terminate();s.hub.wait(timeout=15);time.sleep(.5);s.start_hub();s.login()
    # Persisted output can advance before shutdown; it does not prove that
    # the Agent has reconnected to the replacement Hub process.
    wait_for(lambda:any(d['id']==s.device and d['online'] for d in s.must(s.client.get('/api/devices'))['devices']),20)
    wait_for(lambda:len(read(s,sid))>len(after),20)
    all_output=read(s,sid).decode();ticks=[line for line in all_output.splitlines() if line.startswith('TICK:')]
    assert len(ticks)==len(set(ticks))
    # Independent file operations still work while native CLI is alive.
    result=s.fs('fs_read',path='README.md');assert result.get('content') or result.get('pending')
    writer=uuid.uuid4().hex;s.must(call(s,'lease',{'id':sid,'writer':writer}))
    receipt=uuid.uuid4().hex;args={'id':sid,'writer':writer,'receipt':receipt,'text':'UNIQUE-INPUT\n'}
    s.must(call(s,'input',args));s.must(call(s,'input',args));wait_for(lambda:b'ECHO:UNIQUE-INPUT' in read(s,sid))
    assert read(s,sid).count(b'ECHO:UNIQUE-INPUT')==1
    denied=call(s,'lease',{'id':sid,'writer':uuid.uuid4().hex});assert denied.status_code==409
    # No native input/output appears in the regular audit / operation journal.
    with closing(__import__('sqlite3').connect(s.hubdir/'hub.sqlite3')) as db:
        assert not db.execute("SELECT 1 FROM audit WHERE detail LIKE '%UNIQUE-INPUT%'").fetchone()
    s.must(call(s,'stop',{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex}))
    wait_for(lambda:any(x['id']==sid and x['status']=='exited' for x in s.must(s.client.get('/api/native/sessions'))['sessions']))
    s.must(call(s,'clear',{'id':sid,'confirm':sid}));assert read(s,sid)==b''


def test_admin_csrf_mapping_permissions_and_upload(cli_stack):
    s=cli_stack
    with httpx.Client(base_url=s.url,trust_env=False) as client:
        assert client.get('/api/native/sessions').status_code==401
        client.cookies.update(s.client.cookies)
        assert client.post('/api/native/discover',json={'project':s.project['id']}).status_code==403
    sid=start(s)
    wrong=call(s,'rename',{'id':sid,'title':'bad'},s.projects[1]['id']);assert wrong.status_code==403
    raw=b'\x89PNG fake native fixture';fid=uuid.uuid4().hex;sha=hashlib.sha256(raw).hexdigest()
    s.must(call(s,'upload_begin',{'file':fid,'name':'../image name.png','size':len(raw),'sha256':sha}))
    chunk={'file':fid,'offset':0,'data':base64.b64encode(raw).decode(),'sha256':sha}
    r=s.must(call(s,'upload_chunk',chunk));assert r['ready'];assert s.must(call(s,'upload_chunk',chunk))['received']==len(raw)
    assert Path(r['path']).name==fid+'.png'
    folder_path='/api/spaces/'+s.space_id+'/folders'
    def folder_tasks(allowed):
        current=next(f for f in s.must(s.client.get(folder_path))['folders'] if f['id']==s.folder['id'])
        values={key:current[key] for key in ('name','source_kind','device_id','device_path','mode')}
        values.update(allow_tasks=allowed,expected_version=current['version'],idempotency_key=uuid.uuid4().hex)
        path=folder_path+'/'+current['id']
        result=s.client.put(path,json=values)
        if result.status_code==409 and result.json()['error']['code']=='VALIDATION_PENDING':
            original=s.poll(result.json()['error']['operation_id'],timeout=30)
            assert original['state']=='succeeded',original
            result=s.client.put(path,json=values)
        return s.must(result)
    folder_tasks(False)
    unavailable=s.client.get('/api/native/sessions/'+sid+'/output')
    assert unavailable.status_code==409 and unavailable.json()['error']['code']=='DEFAULT_DEVICE_UNAVAILABLE'
    folder_tasks(True)
    s.must(s.client.patch('/api/devices/'+s.device,json={'enabled':False}))
    unavailable=s.client.get('/api/native/sessions/'+sid+'/output')
    assert unavailable.status_code==409 and unavailable.json()['error']['code']=='SOURCE_DEVICE_UNAVAILABLE'
    s.must(s.client.patch('/api/devices/'+s.device,json={'enabled':True}))
    wait_for(lambda:any(d['online'] for d in s.must(s.client.get('/api/devices'))['devices']),20)


def test_current_native_panel_never_mounts_archived_terminal_assets(cli_stack):
    s=cli_stack
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page();assets=[];starts=[]
        page.on('request',lambda request:assets.append(request.url))
        page.on('request',lambda r:starts.append(r.url) if r.method=='POST' and r.url.endswith('/api/native/start') else None)
        try:
            page.goto(s.url+'/#native');page.fill('#username','admin');page.fill('#password',s.password);page.click('#login-form button')
            expect(page.locator('#chat-root')).to_be_visible(timeout=15000)
            expect(page.locator('#native-root')).to_have_count(0)
            assert page.evaluate('typeof NativeCLIUI')=='undefined'
            assert not any(any(term in url for term in ['xterm','native-cli.js','addon-fit']) for url in assets)
            # Removed bookmarks do not restore the old renderer or start a CLI.
            page.evaluate('navigate("terminal")')
            expect(page.locator('#native-root')).to_have_count(0)
            assert starts==[]
        finally:browser.close()
