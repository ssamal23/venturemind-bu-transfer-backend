# BU transfer credit: backend

FastAPI service, background worker, and database migrations for the transfer credit evaluation prototype.

**What it does:** reads a student's transcript, applies BU's rules in code, matches courses to BU equivalents, and for the rest suggests a match from a syllabus with an LLM. A department evaluator makes every final decision, and the credit office posts the result. The AI only suggests.

| Part | Where |
|---|---|
| REST API | `app/api/` |
| Rules engine (grade, quarter conversion, one-year window, program rules) | `app/rules/` |
| LLM gateway (Claude, then Gemini fallback, retries, circuit breaker) | `app/llm/` |
| Per-course workflow with human review pauses (LangGraph) | `app/workflows/` |
| Background worker (transcript parsing, syllabus evaluation) | `app/workers/` |
| Database models and seed | `app/db/`, `alembic/` |
| Tests | `tests/` |

## Requirements

- Python 3.12 or newer
- PostgreSQL 16+ with the `vector` (pgvector) extension. Azure Database for PostgreSQL works; allow `VECTOR` under the `azure.extensions` server parameter.

## Set up

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (source .venv/bin/activate on macOS and Linux)
pip install -r requirements-dev.txt
cp .env.example .env              # then edit .env: DATABASE_URL, JWT_SECRET, and the API keys
```

Never commit `.env`. It holds your database password and API keys. If a password contains `@`, `#` or `/`, URL-encode it in `DATABASE_URL`.

### Database

```bash
alembic upgrade head                     # tables, pgvector, append-only triggers
python -m app.workflows.checkpointer     # workflow checkpoint tables (run once)
python -m app.db.seed                    # catalog, program rules, demo users (needs the workbook, see below)
python -m app.db.seed_content            # course descriptions and embeddings
python -m app.db.seed_demo               # demo students and courses
```

**The sponsor's workbook is not in this repository.** The seed reads `data/BU_Transfer_Program_Course_Match.xlsx`. Put the file there (or point `DATA_DIR` at the folder that holds it) before running `python -m app.db.seed`.

## Run

```bash
python -m uvicorn app.main:app --reload --port 8000      # the API; docs at /docs
python -m app.workers.evaluation_worker                  # the worker (needed for uploads to be processed)
python -m app.llm.check                                  # test your API keys and model names
```

Demo accounts (synthetic; password `demo1234`): `student@demo.edu`, `evaluator@demo.edu`, `office@demo.edu`. **Do not expose a deployment that accepts these without restricting who can reach it.** Set `ENVIRONMENT=production` to make the API refuse unsafe settings.

## Test

```bash
python -m pytest tests -q
```

Most tests run against the real database inside a transaction that is rolled back, and they need the seeded demo data (`seed`, `seed_content`, `seed_demo`). The rules, gateway, validator, storage and startup-guard tests need no database.

## Settings

See `.env.example`. The important ones:

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | PostgreSQL connection (`postgresql+psycopg://...?sslmode=require`) |
| `JWT_SECRET` | Signs login tokens. A random value of 32+ characters |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Providers for the gateway. A provider with no key is skipped |
| `LLM_CHAIN` | Order providers are tried, for example `["claude","gemini"]` |
| `STORAGE_BACKEND` | `local` (default) or `azure` (needed when the API and worker are separate containers) |
| `ENVIRONMENT` | `production` turns on startup safety checks |

## Docker and Azure

The `Dockerfile` builds one image used for the API (default command) and the worker (`python -m app.workers.evaluation_worker`). The Azure runbook is [docs/deployment.md](docs/deployment.md).

## Documentation

The design notes are in [docs/](docs/README.md): the AI pipeline and its safety features, the per-course workflow, the rules and the assumptions to confirm with BU, posting, the database design, and deployment. The React app is in the [frontend repository](https://github.com/ssamal23/venturemind-bu-transfer-frontend).

## Limits

This is a hackathon prototype. Use synthetic data only. Known gaps include: no integration with the student record system, a basic file check instead of antivirus, and rules assumptions that still need confirming with the sponsor.
