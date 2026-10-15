import pytest

import app.ai as ai
from app.ai import AIError
from tests.test_ai import FakeResp

PAYLOAD = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}


def setup(monkeypatch, responses):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(ai, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ai.httpx, "post", post)
    return calls


def test_503_and_429_are_retried_until_success(monkeypatch):
    calls = setup(monkeypatch, [FakeResp(503), FakeResp(429), FakeResp(200, PAYLOAD)])
    assert ai.call_llm("s", "p") == "hello"
    assert len(calls) == 3


def test_persistent_503_gives_clear_error(monkeypatch):
    calls = setup(monkeypatch, [FakeResp(503), FakeResp(503), FakeResp(503)])
    with pytest.raises(AIError) as exc:
        ai.call_llm("s", "p")
    assert "503" in str(exc.value)
    assert len(calls) == 3


def test_bad_request_is_not_retried(monkeypatch):
    calls = setup(monkeypatch, [FakeResp(400)])
    with pytest.raises(AIError):
        ai.call_llm("s", "p")
    assert len(calls) == 1


def test_timeout_is_retried(monkeypatch):
    calls = setup(monkeypatch, [ai.httpx.TimeoutException("slow"), FakeResp(200, PAYLOAD)])
    assert ai.call_llm("s", "p") == "hello"
    assert len(calls) == 2
