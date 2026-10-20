"""Real Hub, built-in IPC and external Agent sharing one filesystem tree.

The RCH executable is a deterministic protocol fixture, not a live remote fleet.
"""
from __future__ import annotations

import sys
import uuid

import pytest

from shared.util import atomic_json
from tests.support import running_stack, wait_for

FAKE_RCH = r'''
import json, os, pathlib, subprocess, sys, uuid
args = sys.argv[1:]
if args[:1] == ['capabilities']:
    print(json.dumps({'success': True, 'data': {'env': ['RCH_REQUIRE_REMOTE']}}))
    sys.exit(0)
if args[:1] == ['--json']: args.pop(0)
state = pathlib.Path(os.environ['RCH_STATE_HOME'])
folder = state / 'job-leases'
folder.mkdir(parents=True, exist_ok=True)
if args == ['jobs']:
    print(json.dumps({'success': True, 'data': {'jobs': [{'lease': json.loads(p.read_text())} for p in folder.glob('*.json')]}}))
    sys.exit(0)
if args[0] == 'exec':
    assert os.environ['RCH_REQUIRE_REMOTE'] == '1'
    argv = args[args.index('--') + 1:]
    wrapper = 'rchw-' + str(uuid.uuid4())
    lease = {'schema_version': 1, 'identity': {'local_wrapper_id': wrapper, 'remote_build_id': 42},
             'strict_remote': True, 'worker_id': 'fixture-worker', 'terminal_acknowledged': False,
             'state': 'running', 'phase': 'execute', 'wrapper_pid': os.getpid()}
    path = folder / (wrapper + '.json')
    path.write_text(json.dumps(lease))
    count = pathlib.Path(__file__).with_suffix('.count')
    with count.open('a') as stream: stream.write('exec\n')
    result = subprocess.run(argv, check=False)
    lease.update(terminal_acknowledged=True, state='finished', phase='finished', exit_code=result.returncode)
    path.write_text(json.dumps(lease))
    sys.exit(result.returncode)
assert args[:1] == ['jobs']
path = folder / (args[2] + '.json')
lease = json.loads(path.read_text())
assert lease['identity']['local_wrapper_id'] == args[2]
if args[1] in ('cancel', 'recover'):
    lease.update(terminal_acknowledged=True, state='finished', phase='finished', exit_code=130)
    path.write_text(json.dumps(lease))
print(json.dumps({'success': True, 'data': lease}))
'''


class RoutingFixture:
    def __init__(self, stack):
        self.s = stack
        self.client = stack.client
        self.fake = stack.directory / 'fake-rch'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE_RCH)
        self.fake.chmod(0o700)
        stack.stop_agent()
        stack.config['shell'] = {'enabled': True, 'projects': ['*']}
        stack.config['rch'] = {'enabled': True, 'executable': str(self.fake)}
        atomic_json(stack.config_path, stack.config)
        stack.start_agent()
        self.builtin = next(d for d in self.get('/api/devices')['devices'] if d.get('builtin'))['id']
        current = self.get(f'/api/devices/{self.builtin}/roots')
        saved = self.put(f'/api/devices/{self.builtin}/roots', {'revision': current['revision'],
            'roots': [{'path': str(stack.root), 'writable': True}]})
        wait_for(lambda: self.get(f'/api/devices/{self.builtin}/roots')['status'] == 'applied', timeout=45)
        assert saved
        self.space_path='/api/spaces/'+stack.space_id
        self.put(self.space_path+'/devices/'+stack.device,{'enabled':False,'expected_version':1,'idempotency_key':uuid.uuid4().hex})
        self.pool=self.post('/api/storage-pools',{'name':'Shared test storage','abstract_base':'/storage/shared-test',
            'default_fs_device_id':self.builtin,'default_shell_device_id':stack.device,
            'confirmed_same_storage':True,'mappings':[{'device_id':d,'path':str(stack.root)} for d in (self.builtin,stack.device)],
            'expected_version':0,'idempotency_key':uuid.uuid4().hex})
        self.mappings={m['device_id']:m for m in self.get('/api/storage-pools/'+self.pool['id']+'/mappings')['mappings']}
        self.folder=self.post(self.space_path+'/sources',{'pool_id':self.pool['id'],'name':'Workspace shared sources',
            'source_kind':'storage','mapping_ids':[m['id'] for m in self.mappings.values()],
            'subpath':'.','mode':'write','allow_tasks':True,
            'expected_version':0,'idempotency_key':uuid.uuid4().hex})
        self.execution=self.put(self.space_path+'/execution',{'expected_version':0,'idempotency_key':uuid.uuid4().hex,
            'settings':{'hooks':[{'name':'remote-tests','actions':['test'],'handler':'require_rch'}]}})
        self.project_requests=[{'alias':alias,'source_id':self.folder['id'],'subpath':subpath,
            'idempotency_key':uuid.uuid4().hex} for alias,subpath in [('Routed','Imago'),('Routed-Nexus','Nexus')]]
        self.projects=[self.post(self.space_path+'/projects',body) for body in self.project_requests]
        self.project=self.projects[0]

    def request(self, method, path, body=None):
        response = self.client.request(method, path, **({'json': body} if body is not None else {}))
        if response.status_code == 409 and response.json().get('error', {}).get('code') == 'VALIDATION_PENDING':
            original = response.json()['error']['operation_id']
            result = self.s.poll(original, timeout=45)
            assert result['state'] == 'succeeded', result
            response = self.client.request(method, path, json=body)
        return self.s.must(response)

    def get(self, path): return self.request('GET', path)
    def put(self, path, body): return self.request('PUT', path, body)
    def post(self, path, body): return self.request('POST', path, body)
    def delete(self, path): return self.request('DELETE', path)

    def call(self, tool, args, wait=True):
        result = self.s.call(tool, {'project': self.project['id'], **args})
        if result.get('pending') and wait:
            receipt = self.s.poll(result['operation_id'], timeout=45)
            return receipt
        return result

    def exec(self, argv=None, *, action='test', key=None, **kwargs):
        args = {'action': action, 'idempotency_key': key or uuid.uuid4().hex, 'yield_seconds': 0, **kwargs}
        if argv is not None: args['argv'] = argv
        result = self.s.call('exec', {'project': self.project['id'], **args})
        return self.s.poll(result['operation_id'], timeout=45)


@pytest.fixture
def routing_stack(tmp_path, monkeypatch):
    # Override the suite's disabled-node default for this temporary test Hub only.
    # pytest restores the environment afterward; deployed Hub settings are untouched.
    monkeypatch.setenv('HUB_BUILTIN_AGENT_ENABLED', 'true')
    with running_stack(tmp_path / 'routing') as stack:
        yield RoutingFixture(stack)
