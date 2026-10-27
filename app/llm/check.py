"""Check that each configured LLM provider works, with one tiny request each.

    python -m app.llm.check

Prints the provider, model, reply, and how long it took, or the reason it failed.
Uses the keys and model names in backend/.env. Costs a fraction of a cent.
"""

from app.config import settings
from app.llm.providers.base import LLMRequest, ProviderError


def main() -> None:
    req = LLMRequest(system='Reply with exactly this JSON and nothing else: {"ok": true}', user="ping",
                     purpose="check", max_tokens=300)
    providers = []
    if settings.anthropic_api_key:
        from app.llm.providers.claude import ClaudeProvider

        providers.append(ClaudeProvider())
        providers.append(ClaudeProvider(model=settings.claude_small_model, name="claude_small"))
    else:
        print("claude        : skipped (ANTHROPIC_API_KEY is empty in backend/.env)")
    if settings.gemini_api_key:
        from app.llm.providers.gemini import GeminiProvider

        providers.append(GeminiProvider())
    else:
        print("gemini        : skipped (GEMINI_API_KEY is empty in backend/.env)")

    for p in providers:
        try:
            r = p.complete(req)
            print(f"{p.name:13} : OK  model={r.model}  {r.latency_ms} ms  tokens in/out={r.input_tokens}/{r.output_tokens}  reply={r.text.strip()[:60]!r}")
        except ProviderError as e:
            print(f"{p.name:13} : FAILED ({e.kind})  model={p.model}  {str(e)[:150]}")
    print(f"\nChain in use: {settings.llm_chain}")


if __name__ == "__main__":
    main()
