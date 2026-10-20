"""The fixture startup observer is bounded and never hides a dead child."""
from types import SimpleNamespace

import httpx
import pytest

from tests import support


@pytest.fixture
def clock(monkeypatch):
    state = {'now': 0.0, 'sleeps': 0}

    def sleep(delay):
        state['now'] += delay
        state['sleeps'] += 1

    monkeypatch.setattr(support, 'time',
                        SimpleNamespace(monotonic=lambda: state['now'], sleep=sleep))
    return state


def test_agent_observer_accepts_the_same_live_child(clock):
    process = SimpleNamespace(poll=lambda: None)
    calls = []

    def online():
        calls.append(True)
        return len(calls) == 4

    support.wait_for_agent(process, online, timeout=1)
    assert len(calls) == 4 and clock['sleeps'] == 3


@pytest.mark.parametrize('code', [0, 7, -15])
def test_agent_observer_reports_early_exit_without_wait_or_restart(clock, code):
    def forbidden():
        raise AssertionError('online probe should not run for a dead child')

    with pytest.raises(AssertionError, match=f'Fixture Agent exited with code {code}'):
        support.wait_for_agent(SimpleNamespace(poll=lambda: code), forbidden)
    assert clock['sleeps'] == 0


def test_agent_observer_deadline_is_finite(clock):
    with pytest.raises(AssertionError, match='did not become online within 0.3s'):
        support.wait_for_agent(SimpleNamespace(poll=lambda: None), lambda: False, timeout=.3)
    assert clock['now'] < .5 and clock['sleeps'] == 3


def test_agent_observer_can_recover_a_transient_probe_error_without_restarting(clock):
    attempts = []

    def online():
        attempts.append(True)
        if len(attempts) == 1:
            raise httpx.ConnectError('local fixture connection not ready')
        return True

    support.wait_for_agent(SimpleNamespace(poll=lambda: None), online, timeout=1)
    assert len(attempts) == 2 and clock['sleeps'] == 1


@pytest.mark.parametrize('source,expected', [
    ('from tests.support import running_stack', (False, True)),
    ('from tests.support import Stack as LocalStack', (False, True)),
    ('from tests.support import wait_for', (False, False)),
    ('from pathlib import Path', (False, False)),
])
def test_shared_real_stack_uses_integration_pool_without_marking_plain_helpers(tmp_path, source, expected):
    from tests.classification import module_tiers
    module = tmp_path / 'classification_fixture.py'
    module.write_text(source + '\n')
    assert module_tiers(str(module)) == expected


@pytest.fixture
def control_server():
    """An actual keep-alive HTTP peer; no mocked HTTPX success or retry path."""
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state={'connections':0,'requests':[]}
    lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def setup(self):
            super().setup()
            with lock:
                state['connections']+=1
                self.connection_id=state['connections']
        def log_message(self,*args):
            pass
        def handle_request(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            with lock:
                state['requests'].append({'method':self.command,'path':self.path,
                    'connection':self.connection_id,'cookie':self.headers.get('Cookie'),
                    'csrf':self.headers.get('X-RD-CSRF')})
            if self.path=='/accepted-with-lost-response':
                # The peer has already observed the request; the client must
                # propagate uncertainty, not reconnect and replay a POST.
                self.close_connection=True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            status=int(self.path.rsplit('/',1)[-1]) if self.path.startswith('/status/') else 200
            raw=json.dumps({'connection':self.connection_id}).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            if self.path=='/login':self.send_header('Set-Cookie','fixture_session=fixture-only; Path=/')
            self.end_headers();self.wfile.write(raw);self.wfile.flush()
        do_GET=handle_request
        do_POST=handle_request
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield 'http://127.0.0.1:'+str(server.server_port),state
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        assert not thread.is_alive()


def test_control_observer_keeps_authentication_without_reusing_idle_sockets(control_server,monkeypatch):
    url,state=control_server
    monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1')
    monkeypatch.setenv('ALL_PROXY','http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY','')
    with support.control_client(url) as client:
        assert client.post('/login').status_code==200
        client.headers['X-RD-CSRF']='fixture-csrf'
        assert client.get('/first-observation').status_code==200
        assert client.get('/second-observation').status_code==200
    rows=state['requests']
    assert len(rows)==state['connections']==3
    assert len({row['connection'] for row in rows})==3
    assert all(row['cookie']=='fixture_session=fixture-only' and row['csrf']=='fixture-csrf' for row in rows[1:])


@pytest.mark.parametrize('status',[401,403,409,500,503])
def test_control_observer_returns_every_failure_once_without_retry(control_server,status):
    url,state=control_server
    with support.control_client(url) as client:
        response=client.post('/status/'+str(status),json={'fixture':'intent'})
        assert response.status_code==status
    assert len(state['requests'])==state['connections']==1


def test_control_observer_never_replays_accepted_mutation_after_connection_loss(control_server):
    url,state=control_server
    with support.control_client(url) as client:
        with pytest.raises(httpx.TransportError):
            client.post('/accepted-with-lost-response',json={'fixture':'one-intent'})
    assert state['requests']==[{'method':'POST','path':'/accepted-with-lost-response',
        'connection':1,'cookie':None,'csrf':None}]
    assert state['connections']==1
