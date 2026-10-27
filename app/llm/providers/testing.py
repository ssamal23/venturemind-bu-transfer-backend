"""Stand-in providers for tests and for the live failover demo."""

from collections.abc import Callable

from app.llm.providers.base import LLMRequest, LLMResponse, ProviderError


class FakeProvider:
    """Returns scripted text. `reply` can be a string or a function of the request."""

    def __init__(self, name: str = "fake", model: str = "fake-1", reply: str | Callable[[LLMRequest], str] = "{}"):
        self.name = name
        self.model = model
        self.reply = reply
        self.calls: list[LLMRequest] = []

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        text = self.reply(req) if callable(self.reply) else self.reply
        return LLMResponse(text=text, provider=self.name, model=self.model, input_tokens=100, output_tokens=50, latency_ms=5)


class FailingProvider:
    """Always fails the same way. Used to simulate an outage or a quota limit."""

    def __init__(self, name: str = "failing", model: str = "fail-1", kind: str = "rate_limit",
                 retryable: bool = True, retry_after: float | None = None):
        self.name = name
        self.model = model
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after
        self.calls = 0

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise ProviderError(self.kind, "simulated failure", retryable=self.retryable, retry_after=self.retry_after)


class ScriptedProvider:
    """Plays a list of outcomes in order: a string is a reply, an Exception is raised."""

    def __init__(self, outcomes: list[str | Exception], name: str = "scripted", model: str = "script-1"):
        self.name = name
        self.model = model
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        step = self.outcomes.pop(0) if self.outcomes else self.outcomes_default()
        if isinstance(step, Exception):
            raise step
        return LLMResponse(text=step, provider=self.name, model=self.model, input_tokens=10, output_tokens=10, latency_ms=1)

    @staticmethod
    def outcomes_default() -> str:
        return "{}"
