"""Group 5: Distillation pipeline (tests 22-27) + Group 8 percent/quotes (38)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from distiller import (
    OLLAMA_MODEL,
    distill_error,
    filter_relevant_tracebacks,
    generate_distill_payload,
    is_model_resident,
    is_syntax_error_output,
)


def test_ollama_payload_format():
    payload = generate_distill_payload("ValueError: bad input")
    obj = json.loads(payload)
    assert obj["model"] == OLLAMA_MODEL
    assert obj["stream"] is False
    assert obj["format"] == "json"
    assert "ValueError: bad input" in obj["prompt"]


def test_ollama_payload_escapes_percent_and_quotes():
    """Fix 1 (Group 8 #38): %-chars, quotes, backslashes must round-trip."""
    nasty = 'ValueError: %d format mismatch "embedded \\quotes" \\path'
    payload = generate_distill_payload(nasty)
    obj = json.loads(payload)
    assert nasty in obj["prompt"]


def _async_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_ollama_success_parsing():
    """Mock Ollama returns valid response; distill_error returns dict."""
    response_obj = {
        "response": json.dumps({
            "summary": "Auth failed",
            "file": "auth.py",
            "line_number": 42,
            "exception_type": "ValueError",
        })
    }

    class _Resp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self): return response_obj

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def post(self, *a, **kw): return _Resp()

    with patch("distiller.aiohttp.ClientSession", return_value=_Sess()):
        result = _async_run(distill_error("traceback"))
    assert result is not None
    assert result["exception_type"] == "ValueError"


def test_ollama_timeout_fallback():
    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def post(self, *a, **kw):
            raise asyncio.TimeoutError()

    with patch("distiller.aiohttp.ClientSession", return_value=_Sess()):
        result = _async_run(distill_error("tb"))
    assert result is None


def test_ollama_garbage_json_fallback():
    class _Resp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self): return {"response": "this is not json {{{"}

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def post(self, *a, **kw): return _Resp()

    with patch("distiller.aiohttp.ClientSession", return_value=_Sess()):
        result = _async_run(distill_error("tb"))
    assert result is None


def test_ollama_connection_refused():
    import aiohttp

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def post(self, *a, **kw):
            raise aiohttp.ClientError("refused")

    with patch("distiller.aiohttp.ClientSession", return_value=_Sess()):
        result = _async_run(distill_error("tb"))
    assert result is None


def test_vram_eviction_skips_distillation():
    class _Resp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def json(self): return {"models": [{"name": "other_model"}]}

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, *a, **kw): return _Resp()

    with patch("distiller.aiohttp.ClientSession", return_value=_Sess()):
        assert _async_run(is_model_resident()) is False


# Group 7 tests (32-37)

def test_syntax_error_bypass():
    output = "SyntaxError: unexpected EOF"
    assert is_syntax_error_output(output, exit_code=2) is True


def test_syntax_error_only_on_exit_2():
    """Exit code != 2 should NOT bypass even if SyntaxError appears in output."""
    output = "SyntaxError: unexpected EOF"
    assert is_syntax_error_output(output, exit_code=1) is False


def test_testsuite_fallback_filters_unrelated_tracebacks():
    """Mod auth.py, but pytest traces touch only database.py → discard."""
    tb = "test_database.py FAIL\nFile database.py line 42\nValueError: x"
    result = filter_relevant_tracebacks(tb, "auth.py")
    assert result is None


def test_filter_returns_traceback_when_modified_file_in_trace():
    tb = "test_auth.py FAIL\nFile auth.py line 10\n"
    result = filter_relevant_tracebacks(tb, "auth.py")
    assert result == tb


def test_filter_handles_path_separator_variants():
    tb = "src/auth.py line 10"
    assert filter_relevant_tracebacks(tb, "src\\auth.py") is not None
