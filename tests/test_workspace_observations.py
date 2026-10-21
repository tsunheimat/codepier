"""Fresh Runner reads must not turn stale cache or failures into acceptance."""
from types import SimpleNamespace

import httpx
import pytest

from tests import support, workspace_stack
from tests.workspace_stack import WorkspaceStack


@pytest.fixture
def observer(monkeypatch):
    state = {'now': 0.0, 'requests': []}
    def sleep(delay):
        state['now'] += delay
    clock = SimpleNamespace(monotonic=lambda: state['now'], sleep=sleep)
    monkeypatch.setattr(workspace_stack, 'time', clock)
    monkeypatch.setattr(support, 'time', clock)
    stack = WorkspaceStack.__new__(WorkspaceStack)
    stack.hub = stack.agent = SimpleNamespace(poll=lambda: None)
    clients = []
    def install(handler):
        def request(req):
            state['requests'].append((req.method, req.url.path))
            return handler(req)
        stack.client = httpx.Client(base_url='http://fixture.invalid',
                                   transport=httpx.MockTransport(request))
        clients.append(stack.client)
        return stack, state
    yield install
    for client in clients:
        client.close()


@pytest.mark.parametrize('route', ['settings', 'ssh-hosts'])
@pytest.mark.parametrize('populated', [False, True])
def test_runner_read_requires_freshness_not_plausible_cached_contents(observer, route, populated):
    values = iter([
        {'stale':True, 'status':'cached' if populated else 'unavailable',
         'hosts':[{'host_alias':'fixture'}] if populated else [],
         'configuration':{'name':'plausible old name'} if populated else {}},
        {'hosts':[{'host_alias':'fixture'}], 'configuration':{'name':'fresh name'}}])
    stack, state = observer(lambda req: httpx.Response(200, json=next(values)))
    result = stack.runner_read(route)
    assert result['configuration']['name'] == 'fresh name'
    assert state['requests'] == [('GET', '/api/devices/a/' + route)] * 2
    assert state['now'] == .1


@pytest.mark.parametrize('status', [401, 403, 409, 500, 503])
def test_runner_read_does_not_retry_failed_requests(observer, status):
    stack, state = observer(lambda req: httpx.Response(status, json={'error':'original failure'}))
    with pytest.raises(AssertionError, match=str(status)):
        stack.runner_read('settings')
    assert state['requests'] == [('GET', '/api/devices/a/settings')]
    assert state['now'] == 0


def test_runner_read_does_not_hide_transport_failure(observer):
    def failed(req):
        raise httpx.ReadError('original connection lost', request=req)
    stack, state = observer(failed)
    with pytest.raises(httpx.ReadError, match='original connection lost'):
        stack.runner_read('settings')
    assert state['requests'] == [('GET', '/api/devices/a/settings')]


def test_runner_read_preserves_unexpected_fresh_data_for_assertions(observer):
    # A missing alias in a fresh response is a real defect, not a reason to poll
    # until another response happens to satisfy the test.
    stack, state = observer(lambda req: httpx.Response(200, json={'hosts':[]}))
    assert stack.runner_read('ssh-hosts') == {'hosts':[]}
    assert len(state['requests']) == 1 and state['now'] == 0


def test_runner_read_has_a_bounded_stale_observation_deadline(observer):
    stack, state = observer(lambda req: httpx.Response(200, json={'stale':True, 'status':'offline'}))
    with pytest.raises(AssertionError, match='Fresh Runner ssh-hosts observation timed out.*offline'):
        stack.runner_read('ssh-hosts', timeout=.25)
    assert state['requests'] == [('GET', '/api/devices/a/ssh-hosts')] * 3
    assert .25 <= state['now'] < .4


@pytest.mark.parametrize('child', ['hub', 'agent'])
@pytest.mark.parametrize('code', [0, 7])
def test_runner_read_fails_when_the_original_child_exits(observer, child, code):
    stack, state = observer(lambda req: pytest.fail('dead fixture must not be queried'))
    setattr(stack, child, SimpleNamespace(poll=lambda: code))
    with pytest.raises(AssertionError, match=f'exited with code {code}'):
        stack.runner_read('settings')
    assert state['requests'] == [] and state['now'] == 0


@pytest.mark.parametrize('route', ['apply', 'rollback', 'probe', '../operations'])
def test_runner_read_cannot_repeat_a_mutation(observer, route):
    stack, state = observer(lambda req: pytest.fail('only read endpoints are permitted'))
    with pytest.raises(ValueError, match='Only Runner settings and SSH discovery reads'):
        stack.runner_read(route)
    assert state['requests'] == []


def test_runner_read_observes_the_original_accepted_operation(observer):
    receipts = iter([
        {'state':'running'},
        {'state':'succeeded', 'result':{'ok':True, 'data':{'hosts':[{'host_alias':'fixture'}]}}}])
    def response(req):
        if req.url.path == '/api/devices/a/ssh-hosts':
            return httpx.Response(200, json={'operation_id':'original-read'})
        assert req.url.path == '/api/operations/original-read'
        return httpx.Response(200, json=next(receipts))
    stack, state = observer(response)
    assert stack.runner_read('ssh-hosts')['hosts'] == [{'host_alias':'fixture'}]
    assert state['requests'] == [('GET', '/api/devices/a/ssh-hosts'),
                                 ('GET', '/api/operations/original-read'),
                                 ('GET', '/api/operations/original-read')]
