import pytest

from app.config import settings
from app.llm.gateway import AllProvidersFailed, LLMGateway, build_gateway
from app.llm.providers.base import InvalidOutputError, LLMRequest, ProviderError
from app.llm.providers.testing import FailingProvider, FakeProvider, ScriptedProvider

REQ = LLMRequest(system="s", user="u", purpose="match_reasoning")


def gateway(providers, sleeps=None, **kw):
    sleeps = sleeps if sleeps is not None else []
    kw.setdefault("jitter", lambda: 0.0)
    return LLMGateway(providers, sleep=sleeps.append, **kw), sleeps


def rate_limit(retry_after=None):
    return ProviderError("rate_limit", "429", retryable=True, retry_after=retry_after)


# ---------------------------------------------------------------- retries
def test_success_on_the_first_provider_needs_no_retry_or_fallback():
    g, sleeps = gateway([FakeProvider("claude", reply="ok"), FakeProvider("gemini", reply="backup")])
    out = g.complete(REQ)
    assert out.value == "ok" and not out.fallback_used and sleeps == []
    assert [a.provider for a in out.attempts] == ["claude"]


def test_transient_errors_are_retried_with_growing_backoff():
    p = ScriptedProvider([rate_limit(), rate_limit(), "ok"], name="claude")
    g, sleeps = gateway([p], base_delay=1.0)
    out = g.complete(REQ)
    assert out.value == "ok" and p.calls == 3 and not out.fallback_used
    assert sleeps == [1.0, 2.0]  # 1s then 2s


def test_retry_after_from_the_provider_is_honoured():
    p = ScriptedProvider([rate_limit(retry_after=7), "ok"], name="claude")
    g, sleeps = gateway([p], base_delay=1.0)
    g.complete(REQ)
    assert sleeps == [7.0]


def test_backoff_is_capped():
    p = ScriptedProvider([rate_limit(retry_after=500), "ok"], name="claude")
    g, sleeps = gateway([p], max_delay=20.0)
    g.complete(REQ)
    assert sleeps == [20.0]


def test_jitter_is_added_to_the_backoff():
    p = ScriptedProvider([rate_limit(), "ok"], name="claude")
    g, sleeps = gateway([p], base_delay=2.0, jitter=lambda: 0.5)
    g.complete(REQ)
    assert sleeps == [3.0]  # 2s backoff + 0.5 * 2s jitter


# ---------------------------------------------------------------- failover
def test_falls_over_to_the_second_provider_when_the_first_keeps_failing():
    claude = FailingProvider("claude", kind="rate_limit", retryable=True)
    g, sleeps = gateway([claude, FakeProvider("gemini", reply="from gemini")], max_attempts=3)
    out = g.complete(REQ)
    assert out.value == "from gemini" and out.fallback_used
    assert claude.calls == 3 and len(sleeps) == 2  # tried 3 times, waited between
    assert [(a.provider, a.success, a.fallback_used) for a in out.attempts] == [
        ("claude", False, False), ("claude", False, False), ("claude", False, False), ("gemini", True, True)]


@pytest.mark.parametrize("kind", ["auth", "token_limit", "refused", "bad_request", "truncated"])
def test_errors_retrying_cannot_fix_fail_over_at_once(kind):
    claude = FailingProvider("claude", kind=kind, retryable=False)
    g, sleeps = gateway([claude, FakeProvider("gemini", reply="ok")])
    out = g.complete(REQ)
    assert out.value == "ok" and claude.calls == 1 and sleeps == []  # no wasted retries


def test_token_limit_error_moves_to_the_next_provider():
    big = ScriptedProvider([ProviderError("token_limit", "prompt is too long", retryable=False)], name="claude")
    out = gateway([big, FakeProvider("gemini", reply="handled")])[0].complete(REQ)
    assert out.value == "handled" and out.attempts[0].error_type == "token_limit"


def test_all_providers_failing_raises_and_never_returns_a_value():
    g, _ = gateway([FailingProvider("claude"), FailingProvider("gemini", kind="overloaded")], max_attempts=2)
    with pytest.raises(AllProvidersFailed) as exc:
        g.complete(REQ)
    assert {a.provider for a in exc.value.attempts} == {"claude", "gemini"}
    assert "claude:rate_limit" in str(exc.value)


def test_no_providers_configured_fails_cleanly():
    with pytest.raises(AllProvidersFailed):
        LLMGateway([]).complete(REQ)


# ---------------------------------------------------------------- circuit breaker
def test_circuit_opens_after_repeated_failures_then_skips_the_provider():
    now = [0.0]
    claude = FailingProvider("claude")
    g, _ = gateway([claude, FakeProvider("gemini", reply="ok")], max_attempts=1,
                   breaker_threshold=2, breaker_cooldown_s=60, clock=lambda: now[0])
    g.complete(REQ)
    g.complete(REQ)
    assert g.status()[0]["circuit"] == "open"
    calls_before = claude.calls
    out = g.complete(REQ)
    assert claude.calls == calls_before  # not even tried
    assert out.attempts[0].error_type == "circuit_open" and out.value == "ok"


def test_circuit_lets_one_test_request_through_after_the_cooldown():
    now = [0.0]
    claude = ScriptedProvider([rate_limit(), rate_limit(), "recovered"], name="claude")
    g, _ = gateway([claude, FakeProvider("gemini", reply="backup")], max_attempts=1,
                   breaker_threshold=2, breaker_cooldown_s=60, clock=lambda: now[0])
    g.complete(REQ)
    g.complete(REQ)
    assert g.status()[0]["circuit"] == "open"
    now[0] = 61.0
    assert g.status()[0]["circuit"] == "half-open"
    out = g.complete(REQ)
    assert out.value == "recovered" and g.status()[0]["circuit"] == "closed"


# ---------------------------------------------------------------- invalid answers
def test_invalid_output_gets_one_retry_then_fails_over_without_opening_the_circuit():
    def validate(text):
        if text != "good":
            raise InvalidOutputError("bad shape")
        return text

    claude = ScriptedProvider(["nope", "still nope"], name="claude")
    g, sleeps = gateway([claude, FakeProvider("gemini", reply="good")])
    out = g.complete(REQ, validate=validate)
    assert out.value == "good" and out.fallback_used and claude.calls == 2
    assert g.status()[0]["circuit"] == "closed"  # a wrong answer is not an outage


def test_invalid_output_can_recover_on_the_retry():
    def validate(text):
        if text != "good":
            raise InvalidOutputError("bad")
        return text

    out = gateway([ScriptedProvider(["bad", "good"], name="claude")])[0].complete(REQ, validate=validate)
    assert out.value == "good" and not out.fallback_used


# ---------------------------------------------------------------- logging
def test_every_attempt_is_reported_without_prompt_text():
    seen = []
    g, _ = gateway([FailingProvider("claude", retryable=False), FakeProvider("gemini", reply="x")], on_call=seen.append)
    g.complete(REQ)
    assert [(r.provider, r.success, r.error_type, r.fallback_used) for r in seen] == [
        ("claude", False, "rate_limit", False), ("gemini", True, None, True)]
    assert not any(hasattr(r, "prompt") or hasattr(r, "text") for r in seen)
    assert seen[1].input_tokens == 100 and seen[1].latency_ms == 5


# ---------------------------------------------------------------- configuration
def test_providers_without_keys_are_left_out(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    g = build_gateway()
    assert g.providers == []
    with pytest.raises(AllProvidersFailed):
        g.complete(REQ)


def test_force_primary_fail_simulates_a_claude_outage(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test-not-real")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "llm_chain", ["claude"])
    monkeypatch.setattr(settings, "force_primary_fail", True)
    g = build_gateway()
    assert isinstance(g.providers[0], FailingProvider) and g.providers[0].name == "claude"
    with pytest.raises(AllProvidersFailed) as exc:
        g.complete(REQ)
    assert exc.value.attempts[0].error_type == "rate_limit"


# ---------------------------------------------------------------- Claude provider error mapping
def _claude_with(monkeypatch, exc):
    from app.llm.providers.claude import ClaudeProvider

    p = ClaudeProvider(api_key="sk-test-not-real")

    def boom(**kw):
        raise exc

    monkeypatch.setattr(p._client.messages, "create", boom)
    return p


def _status_error(cls, message):
    e = cls.__new__(cls)  # skip the HTTP-response plumbing; the provider only reads .message
    e.message = message
    return e


@pytest.mark.parametrize("message, kind", [
    ("You have reached your specified workspace API usage limits. You will regain access on 2026-11-01", "quota_exhausted"),
    ("Your credit balance is too low to access the API", "quota_exhausted"),
    ("prompt is too long: 250000 tokens > 200000 maximum", "token_limit"),
    ("messages: field required", "bad_request"),
])
def test_claude_bad_requests_are_classified_and_never_retried(monkeypatch, message, kind):
    import anthropic

    p = _claude_with(monkeypatch, _status_error(anthropic.BadRequestError, message))
    with pytest.raises(ProviderError) as exc:
        p.complete(REQ)
    assert exc.value.kind == kind and exc.value.retryable is False


def test_a_claude_spend_limit_fails_over_to_gemini_in_one_step(monkeypatch):
    import anthropic

    claude = _claude_with(monkeypatch, _status_error(anthropic.BadRequestError, "You have reached your specified workspace API usage limits."))
    g, sleeps = gateway([claude, FakeProvider("gemini", reply="answered by gemini")])
    out = g.complete(REQ)
    assert out.value == "answered by gemini" and out.fallback_used and sleeps == []
    assert out.attempts[0].error_type == "quota_exhausted"
