from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from tests.timing import HANG_GUARD_SECONDS

from mimir.saga import _llm


@pytest.mark.parametrize('provider', ['openai_compat', 'anthropic'])
@pytest.mark.parametrize('sampling,effort', [
    ('default', 'low'), ('custom', 'low'), ('default', None), ('custom', None),
])
def test_api_request_sampling_and_effort(monkeypatch, provider, sampling, effort):
    captured = {}
    if provider == 'openai_compat':
        import requests

        def post(url, **kwargs):
            captured.update(kwargs['json'])
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {'choices': [{'message': {'content': 'ok'}}]})

        monkeypatch.setattr(requests, 'post', post)
        call = _llm._call_openai_compat
    else:
        class Client:
            def __init__(self, **kwargs):
                self.messages = self

            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(content=[SimpleNamespace(text='ok')])

        monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=Client))
        monkeypatch.setattr(_llm, '_ANTHROPIC_CLIENTS', {})
        call = _llm._call_anthropic

    llm = {'provider': provider, 'model': 'fixture-model', 'api_key': 'test-only',
           'url': 'https://api.openai.com/v1/chat/completions', 'sampling': sampling,
           'reasoning_effort': effort}
    assert call(llm, prompt='hi', max_tokens=20, temperature=0.2, system='system') == 'ok'
    assert captured.get('temperature') == (0.2 if sampling == 'custom' else None)
    if provider == 'openai_compat':
        assert captured.get('reasoning_effort') == effort
        assert captured['max_completion_tokens'] == 20
    else:
        assert captured.get('output_config') == ({'effort': effort} if effort else None)
        assert captured['max_tokens'] == 20


@pytest.mark.parametrize('provider', ['openai_compat', 'anthropic'])
def test_no_effort_config_retains_original_request_body(tmp_path, monkeypatch, provider):
    from mimir.saga import _config_io

    path = tmp_path / 'saga.toml'
    path.write_text(f'[llm]\nprovider = "{provider}"\nmodel = "legacy"\n')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    _config_io.install_saga_config(path)
    llm = _config_io.resolve_llm_config('annotation')
    assert llm['reasoning_effort'] is None and llm['sampling'] == 'custom'

    captured = {}
    if provider == 'openai_compat':
        import requests

        def post(url, **kwargs):
            captured.update(kwargs['json'])
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {'choices': [{'message': {'content': 'ok'}}]})

        monkeypatch.setattr(requests, 'post', post)
        call = _llm._call_openai_compat
        expected = {'model': 'legacy', 'messages': [{'role': 'system', 'content': 'system'},
                     {'role': 'user', 'content': 'hi'}], 'temperature': 0.2,
                    'max_completion_tokens': 20}
    else:
        class Client:
            def __init__(self, **kwargs):
                self.messages = self

            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(content=[SimpleNamespace(text='ok')])

        monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=Client))
        monkeypatch.setattr(_llm, '_ANTHROPIC_CLIENTS', {})
        call = _llm._call_anthropic
        expected = {'model': 'legacy', 'max_tokens': 20, 'temperature': 0.2,
                    'system': 'system', 'messages': [{'role': 'user', 'content': 'hi'}]}
    assert call(llm, prompt='hi', max_tokens=20, temperature=0.2, system='system') == 'ok'
    assert captured == expected
    assert json.dumps(captured) == json.dumps(expected)


@pytest.mark.parametrize('toml,expected_effort,expected_sampling', [
    ('', 'low', 'default'),
    ('[llm]\nreasoning_effort = "high"\n', 'high', 'default'),
    ('[llm]\nreasoning_effort = ""\n', None, 'custom'),
    ('[llm]\nmodel = "legacy"\n', None, 'custom'),
    ('[llm]\nprovider = "anthropic"\n', None, 'custom'),
    ('[llm]\napi_key_env = "MY_KEY"\n', None, 'custom'),
    ('[annotation]\ntimeout_seconds = 12\n', None, 'custom'),
    ('[llm]\nreasoning_effort = "low"\nsampling = "custom"\n'
     '[annotation]\nreasoning_effort = ""\nsampling = "default"\n', None, 'default'),
])
def test_resolved_effort_and_sampling(tmp_path, toml, expected_effort, expected_sampling):
    from mimir.saga import _config_io

    path = tmp_path / 'saga.toml'
    path.write_text(toml)
    _config_io.install_saga_config(path)
    resolved = _config_io.resolve_llm_config('annotation')
    assert resolved['reasoning_effort'] == expected_effort
    assert resolved['sampling'] == expected_sampling


@pytest.mark.parametrize('provider,bad', [('openai_compat', 'none'), ('anthropic', 'minimal')])
def test_api_rejects_invalid_effort_before_request(monkeypatch, provider, bad):
    import requests

    monkeypatch.setattr(requests, 'post', lambda *a, **kw: pytest.fail('sent request'))
    monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=lambda **kw: pytest.fail('created client')))
    llm = {'api_key': 'test-only', 'url': 'https://api.openai.com/v1/chat/completions',
           'reasoning_effort': bad}
    call = _llm._call_openai_compat if provider == 'openai_compat' else _llm._call_anthropic
    with pytest.raises(ValueError, match='reasoning effort'):
        call(llm, prompt='hi', max_tokens=20, temperature=0.2, system=None)


@pytest.mark.parametrize('provider', ['openai_compat', 'anthropic'])
def test_unsupported_temperature_warning_has_sampling_hint(monkeypatch, caplog, provider):
    if provider == 'openai_compat':
        import requests

        def failed(*args, **kwargs):
            def raise_error():
                raise RuntimeError('400 Client Error')
            return SimpleNamespace(raise_for_status=raise_error,
                                   text='Unsupported parameter: temperature')

        monkeypatch.setattr(requests, 'post', failed)
        call = _llm._call_openai_compat
    else:
        class Client:
            def __init__(self, **kwargs):
                self.messages = self

            def create(self, **kwargs):
                raise RuntimeError('400 unsupported temperature')

        monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=Client))
        monkeypatch.setattr(_llm, '_ANTHROPIC_CLIENTS', {})
        call = _llm._call_anthropic
    assert call({'api_key': 'test-only', 'sampling': 'custom'}, prompt='hi',
                max_tokens=20, temperature=0.2, system=None) == ''
    assert '[llm] sampling = "default"' in caplog.text


def test_openai_key_never_reaches_non_openai_endpoint(monkeypatch, caplog):
    from mimir.saga import _config_io
    import requests

    values = {"url": "https://integrate.api.nvidia.com/v1/chat/completions?fixture=saga-key-2011",
              "api_key_env": "OPENAI_API_KEY", "provider": "openai_compat"}
    monkeypatch.setattr(_config_io, "get_config", lambda: lambda section, key, default=None:
                        values.get(key, default) if section == "llm" else default)
    monkeypatch.setenv("OPENAI_API_KEY", "test-sensitive-key")
    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("key sent to untrusted host"))
    for _ in range(2):
        llm = _config_io.resolve_llm_config("annotation")
        assert llm["api_key"] == ""
        assert _llm._call_openai_compat(llm, prompt="hello", max_tokens=10, temperature=0, system=None) == ""
    assert caplog.text.count("Refusing OPENAI_API_KEY") == 1
    values["url"] = "https://api.openai.com/v1/chat/completions"
    assert _config_io.resolve_llm_config("annotation")["api_key"] == "test-sensitive-key"
    assert "test-sensitive-key" not in caplog.text


def test_explicit_nvidia_saga_config_keeps_host_specific_key(monkeypatch):
    from mimir.saga import _config_io

    values = {"url": "https://integrate.api.nvidia.com/v1/chat/completions",
              "api_key_env": "NVIDIA_NIM_API_KEY", "model": "mistral-test"}
    monkeypatch.setattr(_config_io, "get_config", lambda: lambda section, key, default=None:
                        values.get(key, default) if section == "llm" else default)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nvidia-test-key")
    effective = _config_io.resolve_llm_config("annotation")
    assert effective["url"] == values["url"]
    assert effective["model"] == values["model"]
    assert effective["api_key"] == "nvidia-test-key"


@pytest.fixture
def claude(monkeypatch):
    state = SimpleNamespace(stage=None, entered=asyncio.Event(), clients=[])

    class AssistantMessage:
        content = [SimpleNamespace(text=" reply ")]

    class Client:
        def __init__(self, **kwargs):
            self.closed = False
            state.clients.append(self)

        async def step(self, stage):
            if state.stage == stage:
                state.entered.set()
                await asyncio.Future()
            if state.stage == stage + "_error":
                raise RuntimeError("broken transport")

        async def connect(self):
            await self.step("connect")

        async def query(self, prompt):
            await self.step("query")

        async def receive_response(self):
            yield AssistantMessage()
            await self.step("response")

        async def disconnect(self):
            self.closed = True
            await self.step("disconnect")

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", SimpleNamespace(
        ClaudeAgentOptions=lambda **kwargs: kwargs,
        ClaudeSDKClient=Client,
        AssistantMessage=AssistantMessage,
    ))
    pool = _llm._AsyncClaudePool(max_size=1, recycle_after=10)
    monkeypatch.setattr(_llm, "_get_async_claude_pool", lambda: pool)
    state.pool = pool
    return state


async def call(timeout=0.02):
    return await _llm.call_llm(
        {"provider": "claude_code", "timeout": timeout}, prompt="hello",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "query", "response"])
async def test_hung_sdk_times_out_and_restores_capacity(claude, stage):
    claude.stage = stage
    # Timeout behaviour is the contract: 2s is below both the 30s default
    # and a 1000x regression of the configured 0.02s budget.
    assert await asyncio.wait_for(call(), 2.0) == ""
    assert claude.entered.is_set()
    assert claude.clients[0].closed
    assert claude.pool.size == 0
    assert claude.pool._idle == []

    claude.stage = None
    assert await asyncio.wait_for(call(), HANG_GUARD_SECONDS) == "reply"
    assert len(claude.clients) == 2
    assert claude.pool.size == 1
    await claude.pool.aclose()


@pytest.mark.asyncio
async def test_acquire_deadline_does_not_discard_borrowed_runner(claude):
    borrowed = await claude.pool.acquire()
    # Timeout behaviour is the contract: 2s is below both the 30s default
    # and a 1000x regression of the configured 0.02s budget.
    assert await asyncio.wait_for(call(), 2.0) == ""
    assert claude.pool.size == 1
    assert claude.clients == []
    await claude.pool.release(borrowed)
    assert await asyncio.wait_for(call(), HANG_GUARD_SECONDS) == "reply"
    assert claude.pool._idle == [borrowed]
    await claude.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "query", "response"])
async def test_cancellation_discards_runner_and_wakes_waiter(claude, stage):
    claude.stage = stage
    task = asyncio.create_task(call(timeout=10))
    await asyncio.wait_for(claude.entered.wait(), HANG_GUARD_SECONDS)
    waiter = asyncio.create_task(claude.pool.acquire())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, HANG_GUARD_SECONDS)
    replacement = await asyncio.wait_for(waiter, HANG_GUARD_SECONDS)
    assert claude.clients[0].closed
    assert replacement._client is None
    assert claude.pool.size == 1
    await claude.pool.release(replacement)
    claude.stage = None
    assert await call() == "reply"
    await claude.pool.aclose()


@pytest.mark.asyncio
async def test_cancelled_acquire_preserves_capacity(claude):
    borrowed = await claude.pool.acquire()
    task = asyncio.create_task(call(timeout=10))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert claude.pool.size == 1
    await claude.pool.release(borrowed)
    assert await call() == "reply"
    await claude.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect_error", "query_error", "response_error"])
async def test_broken_transport_is_not_reused(claude, stage):
    claude.stage = stage
    assert await call() == ""
    assert claude.clients[0].closed
    assert claude.pool.size == 0
    claude.stage = None
    assert await call() == "reply"
    await claude.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_cleanup", [False, True])
async def test_discard_restores_capacity_even_when_close_hangs(claude, cancel_cleanup):
    assert await call() == "reply"
    runner = await claude.pool.acquire()
    claude.stage = "disconnect"
    task = asyncio.create_task(claude.pool.discard(runner, timeout=0.02))
    await asyncio.wait_for(claude.entered.wait(), HANG_GUARD_SECONDS)
    if cancel_cleanup:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        # Must honour discard's 0.02s timeout, not a 20s/30s fallback.
        await asyncio.wait_for(task, 2.0)
    assert claude.pool.size == 0
    assert claude.pool._idle == []
    claude.stage = None
    assert await call() == "reply"
    await claude.pool.aclose()
