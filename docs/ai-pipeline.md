# AI pipeline: embeddings, LLM gateway, evaluation worker

## Flow for one course

```
Student uploads syllabus  ->  jobs row (QUEUED)  ->  course AI_PENDING
        |
Worker claims the job (FOR UPDATE SKIP LOCKED)
  1. safety check the file          (no active content; PDF/DOCX only)
  2. extract text                   (scans with no text go to a human)
  3. redact personal details        (emails, phones, IDs)
  4. embed the text                 (local model, 384 dimensions)
  5. pgvector search                (top 5 BU courses by cosine similarity)
  6. LLM gateway                    (Claude, then Gemini; retries, backoff, circuit breaker)
  7. validate the answer in code    (schema, course must be a candidate, evidence must be grounded)
  8. flag injection-like text       (confidence capped at 0.5, evaluator warned)
  9. save evaluation + llm_calls    (suggestion only)
 10. course -> IN_REVIEW            (a human decides; rules rerun at decision time)
```

Commands, from the repository root:

```bash
python -m app.db.seed_content            # descriptions + embeddings for all 117 courses
python -m app.llm.check                  # test each API key and model name
python -m app.workers.evaluation_worker  # run the worker (add --once to stop when the queue is empty)
python evaluation/run_retrieval.py       # retrieval accuracy report
```

## Embeddings and retrieval

- Model: `all-MiniLM-L6-v2` through fastembed (ONNX). Runs on the machine; no key, and no student text leaves it.
- Course text = title + description + learning outcomes. Descriptions are synthetic ([`data/synthetic/course_content.txt`](../data/synthetic/course_content.txt)), written with different wording at each school.
- Measured on the workbook's 69 known source-to-BU matches ([`evaluation/results/retrieval.json`](../evaluation/results/retrieval.json)): correct course in the **top 3 for 100%**, ranked **first for 83%** (92% for likely-equivalent, 53% for partial matches, as expected).
- **Caveat:** we wrote the descriptions, so this is an optimistic number. Real syllabi are longer and messier. Re-run the script on real or sponsor-supplied syllabi before quoting it as accuracy.

## LLM gateway (`app/llm/gateway.py`)

| Situation | Behaviour |
|---|---|
| Rate limit, overload, timeout, 5xx | Retry the same provider with exponential backoff and jitter, honouring `Retry-After` |
| Spend limit, auth error, token limit, refusal, truncated answer | No retry. Move to the next provider at once |
| Answer fails our checks | One retry on the same provider, then the next provider. Doesn't count against the provider's health |
| Provider keeps failing | Circuit breaker opens for 60 s, then lets one test request through |
| Every provider fails | `AllProvidersFailed`. The job is retried later; after the last retry the case goes to a human marked "no AI suggestion" |

Order is `LLM_CHAIN` in `.env` (default Claude Sonnet 5.5, then Gemini Flash). A provider with no key is skipped. `FORCE_PRIMARY_FAIL=true` simulates a Claude outage for a live demo.

Every attempt is stored in `llm_calls` (provider, model, tokens, latency, error type, fallback used). No prompt or response text is stored there.

## Safety features, and what each one stops

| Feature | Where | Stops |
|---|---|---|
| AI only suggests | `evaluations` vs `evaluation_decisions` | The model approving credit |
| Rules rerun at decision time | `rules/`, `api/decisions.py` | A persuasive suggestion getting credit past grade, deadline or program rules |
| Untrusted-data wrapper and instructions | `llm/matching.py`, `prompts/match_system.txt` | Instructions hidden in a syllabus |
| Tag-breakout removal | `prepare_syllabus` | A syllabus closing our wrapper or opening a fake one |
| Course must be a candidate we offered | `make_validator` | Invented or unrelated course codes |
| Evidence must be grounded | `make_validator` | Answers with no link to the syllabus |
| Injection detection and confidence cap | `core/redaction.py`, worker | An attack that does reach the model getting a high-confidence result |
| PII redaction before the model | `core/redaction.py` | Names of contacts, emails, phones and IDs leaving the system |
| File checks | `api/syllabi.py`, `services/doc_scan.py` | Wrong file types, active PDF content, macros |
| Outage handling | gateway, worker | Students waiting forever, or any auto-approval when AI is down |
| Immutable decisions and audit log | database triggers | After-the-fact edits |

## Known limits

- `doc_scan.py` is a basic check, not a malware scanner. Use Microsoft Defender for Storage or ClamAV in production.
- Redaction removes emails, phones and ID patterns. It does not remove people's names.
- The Gemini free tier may use prompts to improve Google's products. Synthetic data only.
- Prompt injection can't be fully prevented in the model. The design assumes the model can be fooled and limits what a fooled model can do.
- The extracted text of a syllabus is stored (redacted). Set a retention period before using real data.
- No Langfuse tracing yet; `llm_calls` covers the basics.
