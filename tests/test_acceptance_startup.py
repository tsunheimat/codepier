"""Read-only disposable provider startup has bounded, narrow retry semantics."""
import httpx
import pytest

from scripts import check_oidc_authentik as acceptance


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(acceptance.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(acceptance.time, 'sleep', lambda seconds: now.__setitem__(0, now[0]+seconds))
    return now


@pytest.mark.parametrize('status', [502, 503, 504])
def test_readiness_retries_transient_get_and_returns_the_observed_rows(clock, status):
    replies = [httpx.Response(status), httpx.Response(200, json={'results': []}),
               httpx.Response(200, json={'results': [{'slug': 'required', 'pk': 'real-observation'}]})]
    requests = []
    def respond(request):
        requests.append(request)
        assert replies, 'No extra GET after a successful collection may race startup'
        return replies.pop(0)
    with httpx.Client(base_url='http://disposable.invalid', transport=httpx.MockTransport(respond)) as client:
        result = acceptance.await_collection(client, 'flows/instances/?page_size=100',
            lambda rows: any(row.get('slug') == 'required' for row in rows), seconds=10)
    assert result == [{'slug': 'required', 'pk': 'real-observation'}]
    assert len(requests) == 3 and clock[0] == 4
    assert all(request.method == 'GET' and request.url.path == '/api/v3/flows/instances/' for request in requests)


@pytest.mark.parametrize('status', [400, 401, 403, 404, 429, 500])
def test_readiness_does_not_hide_auth_or_unclassified_failures(clock, status):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={'private': 'MUST_NOT_APPEAR_IN_ERROR'})
    with httpx.Client(base_url='http://disposable.invalid', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(AssertionError, match=f'expected 200, got {status}') as failure:
            acceptance.await_collection(client, 'flows/instances/', lambda rows: True)
    assert len(requests) == 1 and clock[0] == 0
    assert 'MUST_NOT_APPEAR_IN_ERROR' not in str(failure.value)


@pytest.mark.parametrize('payload', [None, [], {'results': None}, {'results': {}}, {'results': [1]}])
def test_malformed_success_is_not_retried_as_readiness(clock, payload):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    with httpx.Client(base_url='http://disposable.invalid', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(AssertionError, match='Invalid disposable provider readiness collection'):
            acceptance.await_collection(client, 'flows/instances/', lambda rows: True)
    assert len(requests) == 1 and clock[0] == 0


def test_not_ready_service_hits_deadline_instead_of_being_reported_as_ready(clock):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(503)
    with httpx.Client(base_url='http://disposable.invalid', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError, match='readiness deadline exceeded'):
            acceptance.await_collection(client, 'flows/instances/', lambda rows: True, seconds=5)
    assert len(requests) == 3 and clock[0] == 6


def test_mutation_status_is_never_retried_by_the_readiness_helper(clock):
    request = httpx.Request('POST', 'http://disposable.invalid/api/v3/core/users/')
    with pytest.raises(AssertionError, match='POST /api/v3/core/users/: expected 201, got 503'):
        acceptance.required(httpx.Response(503, request=request), 201)
    assert clock[0] == 0
