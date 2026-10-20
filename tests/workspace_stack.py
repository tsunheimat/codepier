"""Real loopback Hub/Runner with a fresh workspace database, never production state."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx

from tests.workspace_fixtures import WorkspaceFixture
from tests.support import BASE, wait_for_agent, wait_for_hub, wait_for
from shared.util import atomic_json


class WorkspaceStack:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.hub = self.agent = self.client = None
        self.hub_log = self.agent_log = None
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            self.port = server.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.root = self.directory/'source'
        self.project_root = self.root/'team/codepier'
        self.project_root.mkdir(parents=True)
        (self.project_root/'README.md').write_text('Workspace integration fixture\n')
        (self.root/'team/other').mkdir()
        self.hubdir = self.directory/'hub'
        seed = WorkspaceFixture(self.hubdir)
        secret = 'integration-device-secret-' + uuid.uuid4().hex
        with seed.store.transaction():
            seed.store.db.execute('UPDATE storage_mappings SET path=?', (str(self.root),))
            seed.store.db.execute("UPDATE storage_pools SET default_fs_device_id='a'")
            seed.store.db.execute("UPDATE devices SET secret=? WHERE id='a'", (seed.store.encrypt(secret),))
            seed.store.db.execute('''INSERT INTO vps_connections(id,name,name_key,host,owner_user_id,created,updated)
                VALUES(?,?,?,?,?,?,?)''', ('server', 'Saved test SSH server', 'saved test ssh server', 'fixture.invalid', 'alice', time.time(), time.time()))
            seed.store.db.execute('''INSERT INTO space_vps(id,space_id,resource_id,name,name_key,runner_device_id,connection_ref,display_mode)
                VALUES(?,?,?,?,?,?,?,'top_level')''', ('a'*32, 'alice', 'server', 'Test server', 'test server', 'a', 'fixture'))
        self.profile_policy = {'project_rules': [{'projects': ['project'], 'actions': ['read','write','execute']}],
                               'vps_rules': [{'vps': ['a'*32], 'actions': ['read','execute']}]}
        connection, _ = seed.connect(self.profile_policy)
        self.pat = connection['token']
        self.grant_id = connection['grant_id']
        seed.close()
        self.env = {**os.environ, 'HUB_PUBLIC_URL': self.url, 'MCP_PUBLIC_URL': self.url,
                    'HUB_BUILTIN_AGENT_ENABLED': 'false', 'HUB_DATA_DIR': str(self.hubdir), 'PYTHONUNBUFFERED': '1',
                    'OAUTH_REDIRECT_HOSTS': 'client.example,localhost,127.0.0.1'}
        self.hub_log = (self.directory/'hub.log').open('w')
        self.agent_log = (self.directory/'agent.log').open('w')
        self.hub = subprocess.Popen([sys.executable, '-m', 'hub', '--data-dir', str(self.hubdir),
            'run', '--host', '127.0.0.1', '--port', str(self.port)], cwd=BASE, env=self.env,
            stdout=self.hub_log, stderr=subprocess.STDOUT)
        try:
            wait_for_hub(self.hub, self.url)
        except BaseException:
            print((self.directory/'hub.log').read_text())
            raise
        self.client = httpx.Client(base_url=self.url, timeout=35, trust_env=False)
        login = self.must(self.client.post('/api/login', json={'username':'alice', 'password':'a-valid-test-password'},
                                         headers={'Origin':self.url}))
        self.client.headers.update({'X-RD-CSRF':login['csrf'], 'Origin':self.url})
        # A deterministic OpenSSH process fixture, not a claim of a real VPS.
        self.bin = self.directory/'bin'
        self.bin.mkdir()
        fake_ssh = self.bin/'ssh'
        fake_ssh.write_text('#!' + sys.executable + '\nimport os,sys\n'
            'if "--" not in sys.argv or sys.argv[sys.argv.index("--")+1]!="fixture": raise SystemExit(255)\n'
            'os.execv("/bin/sh", ["sh", "-c", sys.argv[-1]])\n')
        fake_ssh.chmod(0o700)
        self.config_path = self.directory/'agent-config/config.json'
        self.config = {'device_id':'a', 'secret':secret, 'name':'Runner a', 'hub_url':self.url,
            'state_dir':str(self.directory/'agent-state'),
            'allowed_roots':[{'path':str(self.root), 'writable':True, 'allow_tasks':True}],
            'shell':{'enabled':True, 'projects':['*'], 'env':{'PATH':str(self.bin)+os.pathsep+os.environ['PATH']}},
            'ssh':{'enabled':True, 'targets':{'fixture':{'host_alias':'fixture'}}}, 'sessions':{'enabled':True}}
        atomic_json(self.config_path, self.config)
        self.start_agent()

    @staticmethod
    def must(response):
        assert response.is_success, f'{response.status_code}: {response.text[:4000]}'
        return response.json()

    def start_agent(self):
        self.agent = subprocess.Popen([sys.executable, '-m', 'agent', '--config', str(self.config_path), 'run'],
            cwd=BASE, env=self.env, stdout=self.agent_log, stderr=subprocess.STDOUT)
        try:
            wait_for_agent(self.agent, lambda: any(d['id']=='a' and d['online']
                           for d in self.client.get('/api/devices').json().get('devices', [])))
        except BaseException:
            print((self.directory/'agent.log').read_text())
            raise

    def restart_hub(self):
        """Restart only this disposable loopback Hub; preserve its DB and Runner.

        This exercises durable in-flight receipt recovery without replaying the
        submitted command or changing either the connection or source mapping.
        """
        self.hub.terminate()
        self.hub.wait(timeout=15)
        self.hub = subprocess.Popen([sys.executable, '-m', 'hub', '--data-dir', str(self.hubdir),
            'run', '--host', '127.0.0.1', '--port', str(self.port)], cwd=BASE, env=self.env,
            stdout=self.hub_log, stderr=subprocess.STDOUT)
        wait_for_hub(self.hub, self.url)
        self.client.close()
        self.client = httpx.Client(base_url=self.url, timeout=35, trust_env=False)
        login = self.must(self.client.post('/api/login', json={'username':'alice', 'password':'a-valid-test-password'},
                                         headers={'Origin':self.url}))
        self.client.headers.update({'X-RD-CSRF':login['csrf'], 'Origin':self.url})
        wait_for_agent(self.agent, lambda: any(d['id']=='a' and d['online']
                       for d in self.client.get('/api/devices').json().get('devices', [])))

    def mcp(self, name, arguments=None, *, token=None, expect_error=False):
        response = self.client.post('/mcp', headers={'Authorization':'Bearer '+(token or self.pat),
            'Accept':'application/json, text/event-stream', 'MCP-Protocol-Version':'2025-11-25'},
            json={'jsonrpc':'2.0','id':uuid.uuid4().hex,'method':'tools/call',
                  'params':{'name':name,'arguments':arguments or {}}})
        body = self.must(response)
        assert 'error' not in body, body
        result = body['result']
        # Error receipts (for example a cancelled SSH command observed using
        # its original key) must stay errors, not be rewritten as successes.
        assert bool(result.get('isError')) is expect_error, result
        if 'structuredContent' in result:
            return result['structuredContent']
        return json.loads(result['content'][0]['text'])

    def finish(self, value):
        identifier = value.get('operation_id')
        if not identifier:
            return value
        def result():
            out = self.must(self.client.get('/api/operations/'+identifier))
            if out['state'] in {'queued','running','reconnecting','cancelling'}:
                return None
            assert out['state']=='succeeded', out
            return (out.get('result') or {}).get('data') or out.get('result') or out
        return wait_for(result, timeout=25)

    def execute(self, tool, **arguments):
        arguments.setdefault('idempotency_key', uuid.uuid4().hex)
        return self.finish(self.mcp(tool, arguments))

    def stop_agent(self):
        if self.agent and self.agent.poll() is None:
            self.agent.terminate()
            try: self.agent.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.agent.kill(); self.agent.wait()

    def close(self):
        self.stop_agent()
        if self.hub and self.hub.poll() is None:
            self.hub.terminate()
            try: self.hub.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.hub.kill(); self.hub.wait()
        if self.client: self.client.close()
        if self.hub_log: self.hub_log.close()
        if self.agent_log: self.agent_log.close()


@contextmanager
def workspace_stack(directory):
    stack = WorkspaceStack.__new__(WorkspaceStack)
    try:
        stack.__init__(directory)
        yield stack
    finally:
        stack.close()
