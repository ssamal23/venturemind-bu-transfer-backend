import time

from app.config import settings
from app.llm.providers.base import LLMRequest, LLMResponse, ProviderError

TOKEN_LIMIT_HINTS = ("token count", "exceeds the maximum", "too long", "context")


class GeminiProvider:
    """Gemini (free tier) through google-genai. The backup provider.

    Free-tier prompts may be used by Google to improve its products, so this is for
    synthetic data only. Use a paid tier or another provider for real student data.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None, timeout_s: float = 90.0):
        from google import genai
        from google.genai import types

        self.name = "gemini"
        self.model = model or settings.gemini_model
        self._types = types
        self._client = genai.Client(
            api_key=api_key or settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )

    def complete(self, req: LLMRequest) -> LLMResponse:
        from google.genai import errors

        started = time.monotonic()
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=req.user,
                config=self._types.GenerateContentConfig(
                    system_instruction=req.system,
                    max_output_tokens=req.max_tokens,
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
        except errors.APIError as e:
            code = getattr(e, "code", None) or 0
            msg = str(getattr(e, "message", e))
            if code == 429:
                raise ProviderError("rate_limit", msg, retryable=True) from e
            if code in (401, 403):
                raise ProviderError("auth", msg, retryable=False) from e
            if code == 400:
                kind = "token_limit" if any(h in msg.lower() for h in TOKEN_LIMIT_HINTS) else "bad_request"
                raise ProviderError(kind, msg, retryable=False) from e
            if code == 503:
                raise ProviderError("overloaded", msg, retryable=True) from e
            raise ProviderError("server_error", msg, retryable=code >= 500) from e
        except Exception as e:  # network errors, timeouts
            raise ProviderError("unavailable", f"{type(e).__name__}", retryable=True) from e

        text = resp.text or ""
        if not text:
            raise ProviderError("empty", "the model returned no text", retryable=True)
        usage = getattr(resp, "usage_metadata", None)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
