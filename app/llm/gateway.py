"""LLM gateway: one call site, several providers behind it.

For each request the gateway tries providers in order. A provider that fails in a way
that might clear up (rate limit, overload, timeout) is retried with exponential
backoff and jitter, honouring Retry-After. A provider that keeps failing, or fails in
a way retrying can't fix (auth, token limit, refusal), is skipped and the next one is
tried. A circuit breaker stops sending traffic to a provider that is clearly down.

If every provider fails, AllProvidersFailed is raised. The caller must leave the case
for a human; nothing here ever approves or rejects anything.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app.config import settings
from app.llm.circuit_breaker import CircuitBreaker
from app.llm.providers.base import InvalidOutputError, LLMProvider, LLMRequest, LLMResponse, ProviderError

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class CallRecord:
    """One attempt, for the llm_calls table and Langfuse. Holds no prompt or response text."""

    provider: str
    model: str
    purpose: str
    success: bool
    error_type: str | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    fallback_used: bool  # this attempt was not on the first provider in the chain


@dataclass
class GatewayResult(Generic[T]):
    value: T
    response: LLMResponse
    fallback_used: bool
    attempts: list[CallRecord] = field(default_factory=list)


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[CallRecord]) -> None:
        kinds = ", ".join(f"{a.provider}:{a.error_type}" for a in attempts if not a.success) or "no providers configured"
        super().__init__(f"All LLM providers failed ({kinds})")
        self.attempts = attempts


class LLMGateway:
    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 20.0,
        breaker_threshold: int = 3,
        breaker_cooldown_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        on_call: Callable[[CallRecord], None] | None = None,
    ) -> None:
        self.providers = providers
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._jitter = jitter
        self.on_call = on_call
        self.breakers = {p.name: CircuitBreaker(breaker_threshold, breaker_cooldown_s, clock) for p in providers}

    def status(self) -> list[dict[str, str]]:
        return [{"provider": p.name, "model": p.model, "circuit": self.breakers[p.name].state} for p in self.providers]

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        backoff = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        backoff += self._jitter() * self.base_delay  # jitter spreads out retries from many workers
        return min(max(backoff, retry_after or 0.0), self.max_delay)

    def complete(self, req: LLMRequest, validate: Callable[[str], T] | None = None) -> GatewayResult:
        """Run the request. `validate` turns the text into a checked object or raises InvalidOutputError."""
        attempts: list[CallRecord] = []

        def record(provider: LLMProvider, idx: int, ok: bool, err: str | None, resp: LLMResponse | None) -> None:
            rec = CallRecord(
                provider=provider.name,
                model=provider.model,
                purpose=req.purpose,
                success=ok,
                error_type=err,
                latency_ms=resp.latency_ms if resp else None,
                input_tokens=resp.input_tokens if resp else None,
                output_tokens=resp.output_tokens if resp else None,
                fallback_used=idx > 0,
            )
            attempts.append(rec)
            if self.on_call:
                self.on_call(rec)

        for idx, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            if not breaker.allow():
                log.warning("Skipping %s: circuit open", provider.name)
                record(provider, idx, False, "circuit_open", None)
                continue

            last_err: ProviderError | None = None
            for attempt in range(1, self.max_attempts + 1):
                resp: LLMResponse | None = None
                try:
                    resp = provider.complete(req)
                    value = validate(resp.text) if validate else resp.text
                except InvalidOutputError as e:
                    err = e
                except ProviderError as e:
                    err = e
                else:
                    breaker.record_success()
                    record(provider, idx, True, None, resp)
                    return GatewayResult(value=value, response=resp, fallback_used=idx > 0, attempts=attempts)

                last_err = err
                record(provider, idx, False, err.kind, resp)
                log.warning("%s attempt %d failed: %s", provider.name, attempt, err)
                # A bad answer gets one more try on the same provider, then we move on.
                limit = 2 if isinstance(err, InvalidOutputError) else self.max_attempts
                if not err.retryable or attempt >= limit:
                    break
                self._sleep(self._delay(attempt, err.retry_after))

            # A wrong answer is not an outage, so it doesn't count against the provider's health.
            if not isinstance(last_err, InvalidOutputError):
                breaker.record_failure()

        raise AllProvidersFailed(attempts)


def build_gateway(on_call: Callable[[CallRecord], None] | None = None, **kwargs) -> LLMGateway:
    """Build the gateway from settings. Providers without a key are left out."""
    from app.llm.providers.testing import FailingProvider

    providers: list[LLMProvider] = []
    for name in settings.llm_chain:
        try:
            if name == "claude" and settings.anthropic_api_key:
                from app.llm.providers.claude import ClaudeProvider

                providers.append(ClaudeProvider())
            elif name == "claude_small" and settings.anthropic_api_key:
                from app.llm.providers.claude import ClaudeProvider

                providers.append(ClaudeProvider(model=settings.claude_small_model, name="claude_small"))
            elif name == "gemini" and settings.gemini_api_key:
                from app.llm.providers.gemini import GeminiProvider

                providers.append(GeminiProvider())
            else:
                log.warning("LLM provider %r skipped (unknown name or missing key)", name)
        except Exception as e:  # a broken provider must not stop the others
            log.warning("LLM provider %r could not start: %s", name, e)

    if settings.force_primary_fail and providers:
        first = providers[0]
        providers[0] = FailingProvider(name=first.name, model=first.model, kind="rate_limit", retryable=False)
        log.warning("FORCE_PRIMARY_FAIL is on: %s is simulated as down", first.name)
    return LLMGateway(providers, on_call=on_call, **kwargs)
