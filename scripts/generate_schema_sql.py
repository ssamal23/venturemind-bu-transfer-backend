"""Write the PostgreSQL DDL for the current models to docs/schema.generated.sql, for review.

    python scripts/generate_schema_sql.py          # run from the backend repository root

Needs no database. The migrations are the source of truth for a real database; this file is
a readable picture of what the models describe.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app.db.models import Base  # noqa: E402


def main() -> None:
    dialect = postgresql.dialect()
    parts = ["-- Generated from app/db/models.py by scripts/generate_schema_sql.py. For review only.\n"]
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        parts.extend(str(CreateIndex(i).compile(dialect=dialect)).strip() + ";" for i in table.indexes)
        parts.append("")
    out = ROOT / "docs" / "schema.generated.sql"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {out.relative_to(ROOT)}: {len(Base.metadata.sorted_tables)} tables")


if __name__ == "__main__":
    main()
