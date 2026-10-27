"""Create the limited database user the running app should use, instead of the admin account.

    python scripts/create_app_user.py            # run from the backend folder, as the admin

What it does:
  * creates (or updates the password of) the role `bu_app`, which can log in and nothing else
  * lets it read and write the application's tables, but not create, drop or truncate them
  * takes UPDATE and DELETE away on the tables that must never change (audit log, decisions, postings)
  * covers tables created later by the admin (migrations)

Run it AFTER `alembic upgrade head` and `python -m app.workflows.checkpointer`, because the grants
cover the tables that exist at that moment.

The admin connection comes from DATABASE_URL (backend/.env), or ADMIN_DATABASE_URL if set.
The new password is typed at a hidden prompt. It is never printed or stored.
Use letters and digits only, 20 or more characters, so it needs no URL encoding.
"""

import getpass
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402

ROLE = "bu_app"
NEVER_CHANGED = ("audit_logs", "evaluation_decisions", "postings")


def main() -> None:
    url = (os.environ.get("ADMIN_DATABASE_URL") or settings.database_url).replace("postgresql+psycopg://", "postgresql://", 1)
    db_name = re.search(r"/([^/?]+)(\?|$)", url).group(1)
    host = re.search(r"@([^:/]+)", url).group(1)

    password = getpass.getpass(f"New password for {ROLE}: ")
    if len(password) < 16 or password != getpass.getpass("Type it again: "):
        sys.exit("The passwords differ, or are shorter than 16 characters. Nothing was changed.")
    if not re.fullmatch(r"[A-Za-z0-9]+", password):
        sys.exit("Use letters and digits only, so the password needs no URL encoding. Nothing was changed.")

    role, db = sql.Identifier(ROLE), sql.Identifier(db_name)
    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_roles where rolname = %s", (ROLE,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL("{} ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
            sql.SQL(verb), role, sql.Literal(password)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role))
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(role))
        conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(role))
        for table in NEVER_CHANGED:
            conn.execute(sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON {} FROM {}").format(sql.Identifier(table), role))

        def can(privilege: str, table: str) -> bool:
            return conn.execute("select has_table_privilege(%s, %s, %s)", (ROLE, f"public.{table}", privilege)).fetchone()[0]

        print(f"\n{verb.title()}d role {ROLE} on database {db_name}.\n")
        print("Checks:")
        print(f"  reads and writes courses            : {can('SELECT', 'courses') and can('UPDATE', 'courses')}")
        print(f"  adds to the audit log               : {can('INSERT', 'audit_logs')}")
        for table in NEVER_CHANGED:
            print(f"  can change or delete {table:<18}: {can('UPDATE', table) or can('DELETE', table)}   (should be False)")
        print(f"  can truncate courses                : {can('TRUNCATE', 'courses')}   (should be False)")
        creates = conn.execute("select has_schema_privilege(%s, 'public', 'CREATE')", (ROLE,)).fetchone()[0]
        print(f"  can create tables                   : {creates}   (should be False)")

    print("\nNext: put this in the API and worker settings (replace <password>):")
    print(f"  DATABASE_URL=postgresql+psycopg://{ROLE}:<password>@{host}:5432/{db_name}?sslmode=require")
    print("Keep the admin account for migrations only.")


if __name__ == "__main__":
    main()
