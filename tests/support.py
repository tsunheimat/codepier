"""Real loopback Hub + outbound Agent used by integration and browser tests."""
from __future__ import annotations
import contextlib, json, os, socket, subprocess, sys, time, uuid
from pathlib import Path
import httpx
from hub.store import Store
from hub import iam
from shared.crypto import password_hash, token
from shared.util import atomic_json

BASE = Path(__file__).resolve().parent.parent


def control_client(url):
    """Independent fixture setup/assertions, not the browser's HTTP transport.

    These observations pause while real browser actions run. Keep authentication
    cookies, but not idle TCP sockets that can race the server's keep-alive close.
    No request retry is installed: a reset after a submitted mutation still fails
    immediately rather than sending the mutation again or fabricating a result.
    """
    return httpx.Client(base_url=url, timeout=35, trust_env=False,
                        limits=httpx.Limits(max_keepalive_connections=0))


def wait_for(fn, timeout=12):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        try:
            result=fn()
            if result: return result
        except Exception as exc: last=exc
        time.sleep(.1)
    raise AssertionError(f"Condition timed out: {last}")

def wait_for_hub(process,url,timeout=45):
    """Probe the same owned child within a bounded cold-import budget.

    Concurrent Chromium/WebKit matrices can starve a cold Python import beyond
    12 seconds. Never restart a failed child or query an environmental proxy.
    """
    end=time.monotonic()+timeout
    last='no response'
    with httpx.Client(timeout=1,trust_env=False) as client:
        while time.monotonic()<end:
            code=process.poll()
            if code is not None:
                raise AssertionError(f'Fixture Hub exited with code {code}; inspect its hub.log')
            try:
                response=client.get(url+'/healthz')
                last=f'HTTP {response.status_code}: {response.text[:160]}'
                data=response.json()
                if response.status_code==200 and isinstance(data,dict) and data.get('status')=='ok':
                    return
            except (httpx.HTTPError,ValueError) as exc:
                last=type(exc).__name__
            time.sleep(.1)
    raise AssertionError(f'Fixture Hub did not become healthy within {timeout}s: {last}; inspect its preserved hub.log')


def wait_for_agent(process, is_online, timeout=45):
    """Observe one owned Agent through bounded cold-start/reconnect latency."""
    end = time.monotonic() + timeout
    last = 'not online'
    while time.monotonic() < end:
        code = process.poll()
        if code is not None:
            raise AssertionError(f'Fixture Agent exited with code {code}; inspect its agent.log')
        try:
            if is_online():
                return
            last = 'not online'
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(.1)
    raise AssertionError(f'Fixture Agent did not become online within {timeout}s: {last}; inspect its preserved agent.log')


class Stack:
    def __init__(self, directory, *, space_label=None):
        self.directory=Path(directory); self.directory.mkdir(parents=True,exist_ok=True)
        with socket.socket() as s:
            s.bind(('127.0.0.1',0)); self.port=s.getsockname()[1]
        self.url=f'http://127.0.0.1:{self.port}'
        self.password='Integration-only-'+token(18)
        self.root=self.directory/'projects'; self.root.mkdir()
        self.imago=self.root/'Imago'; self.imago.mkdir()
        self.nexus=self.root/'Nexus'; self.nexus.mkdir()
        self.lumen=self.root/'Lumen'; self.lumen.mkdir()
        for path in (self.imago,self.nexus,self.lumen):
            (path/'src').mkdir(); (path/'tests').mkdir()
            (path/'README.md').write_text('# '+path.name+'\n\nIntegration fixture, not user repository.\n',encoding='utf-8')
            (path/'src'/'main.py').write_text('"""Local development workspace."""\n\ndef greeting(name: str) -> str:\n    return f"Hello, {name}!"\n\nif __name__ == "__main__":\n    print(greeting("CodePier"))\n',encoding='utf-8')
            (path/'tests'/'test_main.py').write_text('def test_example():\n    assert 2 + 2 == 4\n',encoding='utf-8')
            (path/'.env').write_text('SECRET=never-return-this\n')
            subprocess.run(['git','init','-q',str(path)],check=True,capture_output=True)
            subprocess.run(['git','-C',str(path),'add','README.md','src','tests'],check=True,capture_output=True)
            subprocess.run(['git','-C',str(path),'-c','user.name=CodePier Test','-c','user.email=codepier@example.invalid','commit','-qm','Initial test fixture'],check=True,capture_output=True)
        self.hubdir=self.directory/'hub-data'
        store=Store(self.hubdir)
        self.user_id=uuid.uuid4().hex
        with store.transaction():
            store.db.execute('INSERT INTO users VALUES (?,?,?,?)',(self.user_id,'admin',password_hash(self.password),time.time()))
            iam.create_personal_space(store,self.user_id,'Account workspace')
        store.close()
        self.hub_log=(self.directory/'hub.log').open('w'); self.agent_log=(self.directory/'agent.log').open('w')
        self.env={**os.environ,'HUB_PUBLIC_URL':self.url,'MCP_PUBLIC_URL':self.url,'HUB_DATA_DIR':str(self.hubdir),'PYTHONUNBUFFERED':'1'}
        self.hub=None; self.agent=None
        self.start_hub()
        self.client=control_client(self.url)
        self.login()
        # Test accounts have no ambient workspace. Select an explicit fresh
        # resource container and use it in each setup URL, never in a header.
        self.space_id=self.must(self.client.post('/api/spaces',json={
            'label':space_label or 'Integration workspace','idempotency_key':uuid.uuid4().hex}))['id']
        r=self.client.post('/api/devices',json={'name':'HOME / DEVELOPMENT NODE','hub_url':self.url}); self.must(r)
        self.pairing=r.json()['pairing']; self.device=self.pairing['device_id']
        self.config_path=self.directory/'agent-config'/'config.json'
        self.config={**self.pairing,'state_dir':str(self.directory/'agent-state'),'allowed_roots':[{'path':str(self.root),'writable':True,'allow_tasks':True}],
          'shell':{'enabled':True,'projects':['Imago','Nexus','Lumen']},
          'tasks':{
            'smoke':{'command':[sys.executable,'-u','-c','print("PASS: local task completed")'],'projects':['Imago','Nexus','Lumen'],'timeout':15,'description':'本机示例检查'},
            'slow':{'command':[sys.executable,'-u','-c','import time; print("STARTED",flush=True); time.sleep(20); print("DONE")'],'projects':['Imago'],'timeout':60},
            'timeout':{'command':[sys.executable,'-u','-c','import time; print("WAIT"); time.sleep(10)'],'projects':['Imago'],'timeout':1},
            'failure':{'command':[sys.executable,'-u','-c','import sys; print("EXPECTED FAILURE"); sys.exit(3)'],'projects':['Imago'],'timeout':5}
          }}
        atomic_json(self.config_path,self.config); self.start_agent()
        self.must(self.client.put(f'/api/spaces/{self.space_id}/devices/{self.device}',json={
            'expected_version':0,'idempotency_key':uuid.uuid4().hex}))
        self.folder_resource=self.create_folder({'name':'Integration sources',
            'device_id':self.device,'device_path':str(self.root),'mode':'write','allow_tasks':True})
        self.folder=self.create_source({'name':'Integration sources','source_kind':'folder','folder_resource_id':self.folder_resource['id'],'mode':'write','allow_tasks':True})
        self.projects=[]
        for p in (self.imago,self.nexus,self.lumen):
            self.projects.append(self.create_project({'alias':p.name,'source_id':self.folder['id'],'subpath':p.name,
                'description':p.name+' 本地联调示例'}))
        self.project=self.projects[0]
        self.profile=self.create_profile({'project_rules':[{'projects':[p['id'] for p in self.projects],
            'actions':['read','write','execute']}]},label='Integration project access')
        created=self.create_connection(self.profile)
        self.pat=created['token'];self.grant=created['connection_id']
    @staticmethod
    def must(r):
        assert r.is_success, f'{r.status_code}: {r.text[:1000]}'
        return r.json()
    def validated_create(self, kind, body):
        # Inputs are the new model's actual schema. No old-field or endpoint
        # translation occurs in this fixture. Retry only the same saved request
        # after observing its original validation operation.
        body={'idempotency_key':uuid.uuid4().hex,**body}
        path='/api/folders' if kind=='inventory-folder' else f'/api/spaces/{self.space_id}/{kind}'
        response=self.client.post(path,json=body)
        if response.status_code==409 and response.json().get('error',{}).get('code')=='VALIDATION_PENDING':
            operation=self.poll(response.json()['error']['operation_id'],timeout=45)
            assert operation['state']=='succeeded',operation
            response=self.client.post(path,json=body)
        return self.must(response)
    def create_folder(self, body):
        return self.validated_create('inventory-folder',body)
    def create_source(self, body):
        return self.validated_create('sources',body)
    def create_project(self, body):
        return self.validated_create('projects',body)
    def create_profile(self, policy, *, label=None):
        return self.must(self.client.post(f'/api/spaces/{self.space_id}/profiles',json={
            'label':label or 'Integration '+uuid.uuid4().hex,'policy':policy,'idempotency_key':uuid.uuid4().hex}))
    def create_connection(self, profile, *, live=False, external=False):
        return self.must(self.client.post(f'/api/spaces/{self.space_id}/connections',json={
            'name':'Integration connection','profile_id':profile['id'],'profile_version':profile['version'],
            'consent_mode':'live' if live else 'fixed','confirm_live_profile':live,
            'confirm_external_mcp':external,'idempotency_key':uuid.uuid4().hex}))
    def login(self):
        r=self.client.post('/api/login',json={'username':'admin','password':self.password});self.must(r);self.client.headers['X-RD-CSRF']=r.json()['csrf']
    def start_hub(self):
        self.hub=subprocess.Popen([sys.executable,'-m','hub','--data-dir',str(self.hubdir),'run','--host','127.0.0.1','--port',str(self.port)],cwd=BASE,env=self.env,stdout=self.hub_log,stderr=subprocess.STDOUT)
        try:
            wait_for_hub(self.hub,self.url)
        except AssertionError as exc:
            from shared.audit_redaction import redact_text
            details=redact_text((self.directory/'hub.log').read_text(encoding='utf-8',errors='replace'))[-6000:]
            raise AssertionError(f'{exc}\n{details}') from exc
    def start_agent(self):
        # A killed child can remain online in the Hub until its socket closes.
        # Drain that connection before accepting online as the new child's readiness.
        if self.agent is not None and self.agent.poll() is not None:
            wait_for(lambda:not any(d['id']==self.device and d['online'] for d in self.client.get('/api/devices').json().get('devices',[])),timeout=16)
        self.agent=subprocess.Popen([sys.executable,'-m','agent','--config',str(self.config_path),'run'],cwd=BASE,env=self.env,stdout=self.agent_log,stderr=subprocess.STDOUT)
        try:
            wait_for_agent(self.agent, lambda:any(d['id']==self.device and d['online']
                           for d in self.client.get('/api/devices',timeout=1).json().get('devices',[])))
        except AssertionError as exc:
            from shared.audit_redaction import redact_text
            details=redact_text((self.directory/'agent.log').read_text(encoding='utf-8',errors='replace'))[-6000:]
            raise AssertionError(f'{exc}\n{details}') from exc
    def stop_agent(self):
        if getattr(self,'agent',None) and self.agent.poll() is None:
            self.agent.terminate()
            try:self.agent.wait(timeout=8)
            except subprocess.TimeoutExpired:self.agent.kill();self.agent.wait()
    def call(self,name,args=None,raw=False):
        r=self.client.post('/api/tools/call',json={'tool':name,'arguments':args or {}})
        return r if raw else self.must(r)
    def fs(self,name,**args): return self.call(name,{'project':self.project['id'],**args})
    def rpc(self,method,params=None,token_value=None,id=1,headers=None):
        h={'Accept':'application/json, text/event-stream','Content-Type':'application/json','Authorization':'Bearer '+(token_value or self.pat),'MCP-Protocol-Version':'2025-11-25',**(headers or {})}
        return self.client.post('/mcp',headers=h,json={'jsonrpc':'2.0','id':id,'method':method,'params':params or {}})
    def mcp(self,name,args=None,token_value=None):
        r=self.rpc('tools/call',{'name':name,'arguments':args or {}},token_value);self.must(r);return r.json()['result']
    def poll(self,id,timeout=8):
        return wait_for(lambda:(lambda r:r if r['state'] not in {'running','queued','unknown','reconnecting','cancelling'} else None)(self.client.get('/api/operations/'+id).json()),timeout)
    def close(self):
        self.stop_agent()
        if getattr(self,'hub',None) and self.hub.poll() is None:
            self.hub.terminate()
            try:self.hub.wait(timeout=12)
            except subprocess.TimeoutExpired:self.hub.kill();self.hub.wait()
        if hasattr(self,'client'):self.client.close()
        for name in ('hub_log','agent_log'):
            handle=getattr(self,name,None)
            if handle:handle.close()

@contextlib.contextmanager
def running_stack(directory, *, space_label=None):
    s=Stack.__new__(Stack)
    try:
        s.__init__(directory,space_label=space_label)
        yield s
    finally:s.close()
