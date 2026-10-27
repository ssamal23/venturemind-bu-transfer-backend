import time

import anthropic

from app.config import settings
from app.llm.providers.base import LLMRequest, LLMResponse, ProviderError

TOKEN_LIMIT_HINTS = ("prompt is too long", "too many tokens", "context window", "maximum context")


class ClaudeProvider:
    """Claude through the official Anthropic SDK.

    The SDK's own retries are turned off (max_retries=0): the gateway owns retry,
    backoff and failover, so a failure is reported once and handled in one place.
    """

    def __init__(self, model: str | None = None, name: str = "claude", api_key: str | None = None, timeout: float = 90.0):
        self.name = name
        self.model = model or settings.claude_model
        self._client = anthropic.Anthropic(api_key=api_key or settings.anthropic_api_key, max_retries=0, timeout=timeout)

    def complete(self, req: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=req.max_tokens,
                system=req.system,
                messages=[{"role": "user", "content": req.user}],
                output_config={"effort": "medium"},
            )
        except anthropic.RateLimitError as e:
            raise ProviderError("rate_limit", e.message, retryable=True, retry_after=_retry_after(e)) from e
        except anthropic.APITimeoutError as e:
            raise ProviderError("timeout", "request timed out", retryable=True) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("unavailable", "could not reach the API", retryable=True) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise ProviderError("auth", e.message, retryable=False) from e
        except anthropic.BadRequestError as e:
            low = e.message.lower()
            if "usage limit" in low or "credit balance" in low:
                kind = "quota_exhausted"  # the account's spend limit; retrying is pointless until it resets
            elif any(h in low for h in TOKEN_LIMIT_HINTS):
                kind = "token_limit"
            else:
                kind = "bad_request"
            raise ProviderError(kind, e.message, retryable=False) from e
        except anthropic.APIStatusError as e:
            # 529 is "overloaded"; every 5xx is worth another try
            retryable = e.status_code >= 500 or e.status_code == 408
            raise ProviderError("overloaded" if e.status_code == 529 else "server_error", e.message,
                                retryable=retryable, retry_after=_retry_after(e)) from e

        if msg.stop_reason == "refusal":
            raise ProviderError("refused", "the model declined this request", retryable=False)
        if msg.stop_reason == "max_tokens":
            raise ProviderError("truncated", "the answer was cut off", retryable=False)

        text = "".join(block.text for block in msg.content if block.type == "text")
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        return float(e.response.headers.get("retry-after"))
    except (TypeError, ValueError, AttributeError):
        return None
