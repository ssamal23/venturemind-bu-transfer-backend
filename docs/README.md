# Documentation

Design notes for the BU transfer credit prototype. Start with the first two.

| Document | What it covers |
|---|---|
| [ai-pipeline.md](ai-pipeline.md) | How a syllabus becomes an AI suggestion: embeddings, retrieval, the LLM gateway with Claude-to-Gemini failover, validation, and the safety features |
| [workflow.md](workflow.md) | The per-course LangGraph workflow, its two human review pauses, and how it survives a restart |
| [transcript-parsing.md](transcript-parsing.md) | Reading a transcript, and why the model is only trusted to read |
| [rules.md](rules.md) | The deterministic rules (grade, quarter conversion, one-year window, program rules) and the assumptions still to confirm with BU |
| [posting.md](posting.md) | The credit office: posting results as grade T, with its safeguards and assumptions |
| [database-design.md](database-design.md) | The 19 tables, the ER diagram, the status lifecycle, the design rules, and the migrations |
| [deployment.md](deployment.md) | Deploying to Azure: repositories, identity, containers, and a lock-down checklist |
| [schema.generated.sql](schema.generated.sql) | The DDL the models describe, for review |

The React app is in the [frontend repository](https://github.com/ssamal23/venturemind-bu-transfer-frontend).

## Questions for the sponsor

Several documents end with assumptions that were made in code and should be checked with BU. They are numbered so they can be tracked:

- Items 1 to 9 are in [rules.md](rules.md) (credit conversion, grades, deadlines, and what an equivalent earns).
- Items 10 to 13 are in [posting.md](posting.md) (who posts, zero-credit results, and which date stops the clock).

## What is not built

The student record system integration (posting is recorded here only), reminders for evaluators who never respond, department-based assignment, Microsoft Entra sign-in, and antivirus scanning. Each document lists its own limits.
