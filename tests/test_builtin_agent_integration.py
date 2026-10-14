"""The default built-in device runs a real bundled Agent, not a seeded online flag."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from hub.store import Store
from shared.crypto import password_hash, token
from shared.instance_lock import InstanceLock
from tests.support import BASE, wait_for, wait_for_hub

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class BuiltinHub:
    def __init__(self, directory, *, websocket=True):
        self.directory = directory
        self.websocket = websocket
        self.data = directory / 'data'
        self.password = 'Builtin-test-' + token(18)
        self.user = uuid.uuid4().hex
        store = Store(self.data)
        try:
            store.execute('INSERT INTO users VALUES (?,?,?,?)',
                          (self.user, 'admin', password_hash(self.password), time.time()))
            store.execute('UPDATE iam_users SET instance_admin=1 WHERE user_id=?', (self.user,))
        finally:
            store.close()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('CODEPIER_OIDC_') and key != 'HUB_BUILTIN_AGENT_ENABLED'}
        self.env.update(HUB_DATA_DIR=str(self.data), HUB_PUBLIC_URL=self.url,
                        MCP_PUBLIC_URL=self.url, PYTHONUNBUFFERED='1')
        self.log = (directory / 'hub.log').open('w')
        self.process = None
        self.client = httpx.Client(base_url=self.url, timeout=10, trust_env=False)

    def start(self):
        if self.websocket:
            command = [sys.executable, '-m', 'hub', '--data-dir', str(self.data), 'run',
                       '--host', '127.0.0.1', '--port', str(self.port)]
        else:
            command = [sys.executable, '-m', 'uvicorn', 'hub.app:create_app', '--factory',
                       '--host', '127.0.0.1', '--port', str(self.port), '--ws', 'none']
            # Deliberately wrong local-listener settings must not affect private IPC.
            self.env.update(HUB_HOST='127.0.0.2', HUB_PORT='1')
        self.process = subprocess.Popen(command, cwd=BASE, env=self.env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        wait_for_hub(self.process, self.url)
        self.client.cookies.clear()
        response = self.client.post('/api/login', json={'username': 'admin', 'password': self.password})
        assert response.status_code == 200, response.text
        self.client.headers['X-RD-CSRF'] = response.json()['csrf']

    def stop(self, *, abrupt=False):
        if self.process is not None and self.process.poll() is None:
            self.process.kill() if abrupt else self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise

    def devices(self):
        response = self.client.get('/api/devices')
        assert response.status_code == 200, response.text
        return response.json()['devices']

    def online(self):
        devices = self.devices()
        assert len(devices) <= 1, devices
        return next((device for device in devices if device.get('builtin') and device['online']), None)

    def config(self):
        return json.loads((self.data / 'builtin-agent' / 'config.json').read_text())

    def roots(self, device, roots=None, revision=None):
        url = f'/api/devices/{device}/roots'
        response = self.client.get(url) if roots is None else self.client.put(
            url, json={'roots': roots, 'revision': revision})
        assert response.status_code == 200, response.text
        return response.json()

    def applied(self, device, revision):
        settings = self.roots(device)
        return settings if settings['status'] == 'applied' and settings['applied_revision'] == revision else None

    def project(self, device, root, *, alias='Mounted', mode='write'):
        body = {'device_id': device, 'root': str(root), 'alias': alias, 'mode': mode,
                'allow_tasks': False, 'idempotency_key': uuid.uuid4().hex}
        response = self.client.post('/api/projects', json=body)
        if response.status_code == 409 and response.json().get('error', {}).get('code') == 'VALIDATION_PENDING':
            operation = response.json()['error']['operation_id']
            wait_for(lambda: self.client.get('/api/operations/' + operation).json()['state'] not in
                     {'queued', 'running', 'reconnecting', 'cancelling'}, timeout=30)
            response = self.client.post('/api/projects', json=body)
        assert response.status_code == 200, response.text
        return response.json()

    def call(self, tool, **arguments):
        return self.client.post('/api/tools/call', json={'tool': tool, 'arguments': arguments})

    def close(self):
        try:
            self.stop()
        finally:
            self.client.close()
            self.log.close()


@pytest.fixture
def builtin_hub(tmp_path):
    hub = BuiltinHub(tmp_path)
    try:
        hub.start()
        yield hub
    finally:
        hub.close()


def released_state(config):
    try:
        with InstanceLock(Path(config['state_dir']) / '.agent.lock'):
            return True
    except RuntimeError:
        return False


def test_default_device_online_rotation_disable_and_deletion(builtin_hub):
    hub = builtin_hub
    device = wait_for(hub.online, timeout=45)
    identity = device['id']
    assert device['name'] == 'CodePier Hub'
    assert device['owner_user_id'] == hub.user
    assert device['project_count'] == 0
    config = hub.config()
    assert config['allowed_roots'] == [] and config['tasks'] == {}
    assert config['shell']['enabled'] is False
    assert config['hub_url'] == hub.url
    if os.name != 'nt':
        assert (hub.data / 'builtin-agent' / 'config.json').stat().st_mode & 0o777 == 0o600
        assert (hub.data / 'builtin-agent').stat().st_mode & 0o777 == 0o700
    secret = config['secret']
    response = hub.client.post(f'/api/devices/{identity}/rotate')
    assert response.status_code == 200, response.text
    assert response.json().get('builtin') is True and 'pairing' not in response.json()
    assert secret not in response.text
    wait_for(lambda: hub.config()['secret'] != secret, timeout=20)
    assert wait_for(hub.online, timeout=45)['id'] == identity
    rotated = hub.config()

    response = hub.client.patch(f'/api/devices/{identity}', json={'enabled': False})
    assert response.status_code == 200, response.text
    wait_for(lambda: not hub.devices()[0]['online'])
    wait_for(lambda: released_state(rotated), timeout=20)
    hub.stop()
    hub.start()
    assert not hub.devices()[0]['enabled']
    assert hub.devices()[0]['id'] == identity
    response = hub.client.patch(f'/api/devices/{identity}', json={'enabled': True})
    assert response.status_code == 200, response.text
    assert wait_for(hub.online, timeout=45)['id'] == identity
    assert hub.config()['secret'] == rotated['secret']

    response = hub.client.delete(f'/api/devices/{identity}')
    assert response.status_code == 200, response.text
    wait_for(lambda: released_state(rotated), timeout=20)
    assert hub.devices() == []
    hub.stop()
    hub.start()
    # Read durable state as well as the list so startup timing cannot mask recreation.
    store = Store(hub.data)
    try:
        assignment = store.one("SELECT value FROM meta WHERE key='builtin_agent'")
        assert json.loads(assignment['value'])['device_id'] == identity
        assert not store.one('SELECT id FROM devices WHERE id=?', (identity,))
    finally:
        store.close()
    assert hub.devices() == []


def test_abrupt_hub_exit_releases_agent_and_preserves_identity(builtin_hub):
    hub = builtin_hub
    identity = wait_for(hub.online, timeout=45)['id']
    config = hub.config()
    assert not released_state(config), 'online Agent must own its state lock'
    hub.stop(abrupt=True)
    wait_for(lambda: released_state(config), timeout=20)
    hub.start()
    assert wait_for(hub.online, timeout=45)['id'] == identity
    assert hub.config()['secret'] == config['secret']
    hub.stop()
    wait_for(lambda: released_state(config), timeout=20)


def test_mounted_project_read_write_revocation_and_restart_without_websocket(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    project = workspace / 'project'
    project.mkdir()
    (project / 'README.md').write_text('mounted project\n')
    hub = BuiltinHub(tmp_path, websocket=False)
    try:
        hub.start()
        identity = wait_for(hub.online, timeout=45)['id']
        saved = hub.roots(identity, [{'path': str(workspace), 'writable': True}], revision=0)
        revision = saved['revision']
        wait_for(lambda: hub.applied(identity, revision), timeout=30)
        mapping = hub.project(identity, project)
        response = hub.call('fs_read', project='Mounted', path='README.md')
        assert response.status_code == 200, response.text
        assert response.json()['content'] == 'mounted project\n'
        write_args = {'project': 'Mounted', 'path': 'local.txt', 'content': 'private IPC\n',
                      'expected_sha256': 'new', 'idempotency_key': uuid.uuid4().hex}
        response = hub.call('fs_write', **write_args)
        assert response.status_code == 200, response.text
        first_operation = response.json()['operation_id']
        assert (project / 'local.txt').read_text() == 'private IPC\n'
        assert hub.config()['shell']['enabled'] is False
        assert not hub.config()['allowed_roots'][0]['allow_tasks']
        hub.stop()
        hub.start()
        assert wait_for(hub.online, timeout=45)['id'] == identity
        wait_for(lambda: hub.applied(identity, revision), timeout=30)
        # A durable replay after the Hub/child restart must not repeat the mutation.
        response = hub.call('fs_write', **write_args)
        assert response.status_code == 200, response.text
        assert response.json()['operation_id'] == first_operation
        response = hub.call('fs_read', project='Mounted', path='local.txt')
        assert response.status_code == 200 and response.json()['content'] == 'private IPC\n'
        saved = hub.roots(identity, [{'path': str(workspace), 'writable': False}], revision=revision)
        revision = saved['revision']
        wait_for(lambda: hub.applied(identity, revision), timeout=30)
        response = hub.call('fs_write', **{**write_args, 'path': 'denied.txt', 'idempotency_key': uuid.uuid4().hex})
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'READ_ONLY'
        assert not (project / 'denied.txt').exists()
        saved = hub.roots(identity, [], revision=revision)
        wait_for(lambda: hub.applied(identity, saved['revision']), timeout=30)
        response = hub.call('fs_read', project='Mounted', path='README.md')
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'ROOT_NOT_ALLOWED'
        projects = hub.client.get('/api/projects').json()['projects']
        assert any(row['id'] == mapping['id'] for row in projects)
        assert (project / 'local.txt').read_text() == 'private IPC\n'
    finally:
        hub.close()
