"""Tests for ``mimir.tools.web`` — gating predicate and tool plumbing.

HTTP-side behavior is tested with monkeypatched urllib so no network
calls leak from the suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mimir.tools import web as web_tools_mod
from mimir.tools.web import (
    _name_from_url,
    _provider_from_model_spec,
    _sanitize_download_name,
    web_tools_enabled,
)


# ─── Provider gating ───────────────────────────────────────────────


class TestProviderGating:
    def test_default_blocks_when_provider_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No spec means we fall back to claude_code; web tools off.
        monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("MIMIR_FETCH_URL_DISABLED", raising=False)
        assert web_tools_enabled() == (False, False)

    def test_claude_code_blocks_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with TAVILY_API_KEY set, claude_code provider gets no web tools.
        monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:claude-sonnet-4-6")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        assert web_tools_enabled() == (False, False)

    def test_external_provider_enables_when_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_MODEL_SPEC", "anthropic:claude-haiku-4-5")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        monkeypatch.delenv("MIMIR_FETCH_URL_DISABLED", raising=False)
        assert web_tools_enabled() == (True, True)

    def test_external_provider_no_tavily_key_disables_search_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # fetch_url doesn't need Tavily; only web_search gates on the key.
        monkeypatch.setenv("MIMIR_MODEL_SPEC", "openai:gpt-5.4-nano")
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("MIMIR_FETCH_URL_DISABLED", raising=False)
        assert web_tools_enabled() == (False, True)

    def test_fetch_url_disabled_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_MODEL_SPEC", "anthropic:claude-haiku-4-5")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        monkeypatch.setenv("MIMIR_FETCH_URL_DISABLED", "1")
        assert web_tools_enabled() == (True, False)

    def test_explicit_model_spec_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When passed an explicit spec, env MIMIR_MODEL_SPEC is ignored.
        monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        assert web_tools_enabled(model_spec="anthropic:bar") == (True, True)

    def test_provider_prefix_normalization(self) -> None:
        assert _provider_from_model_spec("claude-code:foo") == "claude_code"
        assert _provider_from_model_spec("claude_code:foo") == "claude_code"
        assert _provider_from_model_spec("anthropic:foo") == "anthropic"
        assert _provider_from_model_spec("openai_compat:gpt-x") == "openai_compat"
        assert _provider_from_model_spec("") == "claude_code"
        assert _provider_from_model_spec(None) == "claude_code"


# ─── Name normalization helpers ────────────────────────────────────


class TestNameHelpers:
    def test_sanitize_strips_unsafe_chars(self) -> None:
        assert _sanitize_download_name("hello world!.txt") == "hello-world-.txt"
        assert _sanitize_download_name("foo/bar/baz.html") == "foo-bar-baz.html"

    def test_sanitize_truncates_long_names(self) -> None:
        long = "a" * 200 + ".html"
        out = _sanitize_download_name(long)
        assert len(out) <= 120
        assert out.endswith(".html")

    def test_sanitize_returns_fallback_for_empty(self) -> None:
        assert _sanitize_download_name("") == "download.bin"
        assert _sanitize_download_name("---") == "download.bin"

    def test_name_from_url_uses_path_name(self) -> None:
        assert _name_from_url("https://example.com/foo/bar.pdf") == "bar.pdf"

    def test_name_from_url_handles_root(self) -> None:
        assert _name_from_url("https://example.com/") == "index.html"
        assert _name_from_url("https://example.com") == "index.html"

    def test_name_from_url_adds_bin_when_no_extension(self) -> None:
        assert _name_from_url("https://example.com/raw-data") == "raw-data.bin"


# ─── fetch_url end-to-end (urllib monkeypatched) ───────────────────


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200, final_url: str = "https://example.com/x") -> None:
        self._body = body
        self._pos = 0
        self._status = status
        self._final_url = final_url
        self.headers = {"Content-Type": "text/html"}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self._status

    def geturl(self) -> str:
        return self._final_url

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            out = self._body[self._pos:]
            self._pos = len(self._body)
            return out
        out = self._body[self._pos : self._pos + n]
        self._pos += len(out)
        return out


def _patch_safe_open(monkeypatch: pytest.MonkeyPatch, response_factory) -> None:
    """Replace _open_url + _validate_fetch_url so tests don't hit network.

    Real SSRF resolution depends on DNS; in CI we don't want test runs
    to be load-bearing on the runner's resolver. ``response_factory`` is
    a callable that returns the fake response each call (so tests can
    drive different bodies per invocation).
    """
    monkeypatch.setattr(web_tools_mod, "_open_url", lambda req, timeout=0: response_factory())
    # Treat every URL as public for the test (real SSRF tests below do
    # NOT patch this — they exercise the actual guard).
    monkeypatch.setattr(web_tools_mod, "_validate_fetch_url", lambda url: None)


async def _drive_fetch_url(tmp_path: Path, body: bytes) -> dict[str, Any]:
    """Helper: call the underlying coroutine of fetch_url and return the parsed meta dict.

    langchain @tool wraps the function so we invoke ``.afunc`` directly.
    """
    web_tools_mod.set_home(tmp_path)
    (tmp_path / "attachments").mkdir(exist_ok=True)
    yaml_str = await web_tools_mod.fetch_url.ainvoke(
        {"url": "https://example.com/foo.html"}
    )
    import yaml as _yaml
    return _yaml.safe_load(yaml_str)


@pytest.mark.asyncio
async def test_fetch_url_writes_body_and_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"<html>hello</html>"
    _patch_safe_open(monkeypatch, lambda: _FakeResponse(body))
    meta = await _drive_fetch_url(tmp_path, body)

    assert meta["url"] == "https://example.com/foo.html"
    assert meta["bytes"] == len(body)
    # Body file under attachments/fetch-cache/
    body_rel = meta["file_path"].lstrip("/")
    assert (tmp_path / body_rel).read_bytes() == body
    # Meta sidecar
    meta_rel = meta["metadata_path"].lstrip("/")
    meta_disk = json.loads((tmp_path / meta_rel).read_text())
    assert meta_disk["url"] == meta["url"]
    assert meta_disk["sha256"] == meta["sha256"]


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http(tmp_path: Path) -> None:
    web_tools_mod.set_home(tmp_path)
    msg = await web_tools_mod.fetch_url.ainvoke({"url": "file:///etc/passwd"})
    # Either the SSRF guard ("http/https URLs allowed") or the legacy
    # scheme check is sufficient — both indicate "non-http rejected".
    lowered = msg.lower()
    assert "http" in lowered and "https" in lowered


@pytest.mark.asyncio
async def test_fetch_url_empty_url(tmp_path: Path) -> None:
    web_tools_mod.set_home(tmp_path)
    msg = await web_tools_mod.fetch_url.ainvoke({"url": "   "})
    assert "url is required" in msg


@pytest.mark.asyncio
async def test_fetch_url_rejects_localhost(tmp_path: Path) -> None:
    web_tools_mod.set_home(tmp_path)
    # Bare ``localhost`` is rejected at the name layer before DNS.
    msg = await web_tools_mod.fetch_url.ainvoke({"url": "http://localhost/x"})
    assert "fetch_url failed" in msg and "not allowed" in msg


@pytest.mark.asyncio
async def test_fetch_url_rejects_private_ip(tmp_path: Path) -> None:
    web_tools_mod.set_home(tmp_path)
    msg = await web_tools_mod.fetch_url.ainvoke({"url": "http://10.0.0.1/x"})
    assert "non-public address" in msg


@pytest.mark.asyncio
async def test_fetch_url_rejects_link_local(tmp_path: Path) -> None:
    # 169.254.169.254 is the AWS/GCP instance metadata service.
    # If this URL slips through SSRF, the agent can exfiltrate
    # cloud credentials from a compromised prompt.
    web_tools_mod.set_home(tmp_path)
    msg = await web_tools_mod.fetch_url.ainvoke({"url": "http://169.254.169.254/latest/meta-data/"})
    assert "non-public address" in msg


@pytest.mark.asyncio
async def test_fetch_url_rejects_loopback(tmp_path: Path) -> None:
    web_tools_mod.set_home(tmp_path)
    msg = await web_tools_mod.fetch_url.ainvoke({"url": "http://127.0.0.1/x"})
    assert "non-public address" in msg


@pytest.mark.asyncio
async def test_fetch_url_blocks_redirect_to_metadata_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An external URL passes initial validation but Location:-redirects
    # to a metadata service URL. The custom redirect handler re-runs
    # the SSRF check on each hop and rejects it.
    web_tools_mod.set_home(tmp_path)
    monkeypatch.setattr(web_tools_mod, "_validate_fetch_url",
                        lambda url: web_tools_mod._validate_fetch_url.__wrapped__(url)
                        if hasattr(web_tools_mod._validate_fetch_url, "__wrapped__")
                        else None if "example.com" in url
                        else (_ for _ in ()).throw(
                            web_tools_mod.SSRFBlocked(f"non-public address (test) for {url}")
                        ))

    # Drive through the actual redirect handler by calling redirect_request directly.
    from urllib.request import Request as _Req
    handler = web_tools_mod._SSRFCheckingRedirectHandler()
    req = _Req("https://example.com/")
    with pytest.raises(web_tools_mod.SSRFBlocked):
        handler.redirect_request(
            req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
        )


@pytest.mark.asyncio
async def test_fetch_url_max_bytes_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_tools_mod.set_home(tmp_path)
    _patch_safe_open(monkeypatch, lambda: _FakeResponse(b"x" * 5_000_000))
    msg = await web_tools_mod.fetch_url.ainvoke(
        {"url": "https://example.com/big", "max_bytes": 1024}
    )
    assert "exceeded max_bytes" in msg


@pytest.mark.asyncio
async def test_fetch_url_content_length_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Server advertises a body larger than max_bytes via Content-Length;
    # we should reject BEFORE reading any of it.
    web_tools_mod.set_home(tmp_path)

    class _BigClaimer(_FakeResponse):
        def __init__(self) -> None:
            super().__init__(b"x")  # body is tiny; the header lies
            self.headers = {"Content-Type": "text/plain", "Content-Length": "9999999"}

    _patch_safe_open(monkeypatch, _BigClaimer)
    msg = await web_tools_mod.fetch_url.ainvoke(
        {"url": "https://example.com/big", "max_bytes": 1024}
    )
    assert "Content-Length" in msg or "max_bytes" in msg


# ─── SSRF helper unit tests (the validator itself) ─────────────────


class TestValidateFetchURL:
    def test_passes_public_dns(self) -> None:
        # example.com is a well-known public host.
        web_tools_mod._validate_fetch_url("https://example.com/")

    def test_blocks_non_http(self) -> None:
        with pytest.raises(web_tools_mod.SSRFBlocked):
            web_tools_mod._validate_fetch_url("file:///etc/passwd")

    def test_blocks_localhost_string(self) -> None:
        with pytest.raises(web_tools_mod.SSRFBlocked):
            web_tools_mod._validate_fetch_url("http://localhost/x")

    def test_blocks_loopback_ip(self) -> None:
        with pytest.raises(web_tools_mod.SSRFBlocked):
            web_tools_mod._validate_fetch_url("http://127.0.0.1/x")

    def test_blocks_private_ip(self) -> None:
        with pytest.raises(web_tools_mod.SSRFBlocked):
            web_tools_mod._validate_fetch_url("http://10.0.0.1/x")

    def test_blocks_metadata_service(self) -> None:
        with pytest.raises(web_tools_mod.SSRFBlocked):
            web_tools_mod._validate_fetch_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_ipv6_loopback(self) -> None:
        with pytest.raises(web_tools_mod.SSRFBlocked):
            web_tools_mod._validate_fetch_url("http://[::1]/x")

    def test_blocks_unspecified(self) -> None:
        with pytest.raises(web_tools_mod.SSRFBlocked):
            web_tools_mod._validate_fetch_url("http://0.0.0.0/x")


# ─── web_search arg validation (no network) ────────────────────────


@pytest.mark.asyncio
async def test_web_search_empty_query() -> None:
    out = await web_tools_mod.web_search.ainvoke({"query": "   "})
    assert "query is required" in out


@pytest.mark.asyncio
async def test_web_search_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = await web_tools_mod.web_search.ainvoke({"query": "anthropic"})
    assert "TAVILY_API_KEY" in out


@pytest.mark.asyncio
async def test_web_search_bad_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    out = await web_tools_mod.web_search.ainvoke(
        {"query": "anthropic", "topic": "sports"}
    )
    assert "topic must be one of" in out


@pytest.mark.asyncio
async def test_web_search_bad_time_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    out = await web_tools_mod.web_search.ainvoke(
        {"query": "anthropic", "time_range": "fortnight"}
    )
    assert "time_range must be one of" in out


# ─── Registry conditional inclusion ────────────────────────────────


def test_all_mimir_tools_omits_web_when_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:claude-sonnet-4-6")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    names = {t.name for t in all_mimir_tools()}
    assert "web_search" not in names
    assert "fetch_url" not in names


def test_all_mimir_tools_includes_web_when_external_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "anthropic:claude-haiku-4-5")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.delenv("MIMIR_FETCH_URL_DISABLED", raising=False)
    names = {t.name for t in all_mimir_tools()}
    assert "web_search" in names
    assert "fetch_url" in names


def test_all_mimir_tools_includes_fetch_only_without_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "openai:gpt-5.4-nano")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("MIMIR_FETCH_URL_DISABLED", raising=False)
    names = {t.name for t in all_mimir_tools()}
    assert "web_search" not in names
    assert "fetch_url" in names
