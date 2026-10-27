"""Common shapes for every LLM provider, so the gateway can swap one for another."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderError(Exception):
    """A provider call failed. `kind` says why; `retryable` says whether trying the same provider again could help."""

    def __init__(self, kind: str, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after


class InvalidOutputError(ProviderError):
    """The model answered, but the answer failed our checks (schema, unknown course, no evidence)."""

    def __init__(self, message: str) -> None:
        super().__init__("invalid_output", message, retryable=True)


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    purpose: str  # for logs: match_reasoning | parse_transcript
    max_tokens: int = 4000


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, req: LLMRequest) -> LLMResponse:
        """Return the model's text. Raise ProviderError on any failure."""
        ...
