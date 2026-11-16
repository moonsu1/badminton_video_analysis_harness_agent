import pytest
import httpx

import badminton.agent as agent


@pytest.mark.parametrize('code', [400, 401, 403, 429, 500])
def test_provider_errors_are_redacted_and_not_retried(system, monkeypatch, code):
    settings, store, _ = system
    monkeypatch.setattr(agent, 'api_key', lambda: 'test-secret')
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(code, json={'error': {'message': 'raw sensitive response test-secret'}})
    real_client = httpx.Client
    monkeypatch.setattr(agent.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    provider = agent.Provider(settings, store, 'run')
    with pytest.raises(RuntimeError) as error:
        provider.create('test', input='test')
    assert 'test-secret' not in str(error.value)
    assert 'raw sensitive' not in str(error.value)
    assert len(calls) == 1
    assert str(calls[0].url) == 'https://api.openai.com/v1/responses'
    assert store.one('SELECT status FROM calls')['status'] == 'failed'


def test_timeout_is_unknown_not_replayed(system, monkeypatch):
    settings, store, _ = system
    monkeypatch.setattr(agent, 'api_key', lambda: 'test-secret')
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout('test timeout')
    real_client = httpx.Client
    monkeypatch.setattr(agent.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(RuntimeError, match='자동 재전송하지'):
        agent.Provider(settings, store, 'run').create('test', input='test')
    assert len(calls) == 1
    assert store.one('SELECT status FROM calls')['status'] == 'unknown'
