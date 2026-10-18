import os
import re
import sys
from pathlib import Path
import psycopg

ROOT_DIR = Path(__file__).resolve().parent.parent

# Read .env directly
env_vars = {}
env_file = ROOT_DIR / ".env"
if env_file.exists():
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip().strip('"').strip("'")

gov_url = env_vars.get("GOVERNANCE_SYNC_DATABASE_URL") or env_vars.get("GOVERNANCE_DATABASE_URL")
staging_url = env_vars.get("STAGING_DATABASE_URL")

if not gov_url or not staging_url:
    print("Error: GOVERNANCE_DATABASE_URL or STAGING_DATABASE_URL missing in .env")
    sys.exit(1)

# Ensure URLs are sync psycopg format (postgresql://)
gov_sync_url = re.sub(r"^postgresql\+[a-zA-Z0-9_]+://", "postgresql://", gov_url)
staging_sync_url = re.sub(r"^postgresql\+[a-zA-Z0-9_]+://", "postgresql://", staging_url)

print(f"Connecting to Governance DB: {gov_sync_url.split('@')[-1]}")
print(f"Connecting to Staging DB: {staging_sync_url.split('@')[-1]}")


def sanitize_sql(sql_content: str) -> str:
    """Strip psql meta-commands like \\connect."""
    cleaned_lines = []
    for line in sql_content.splitlines():
        if line.strip().startswith("\\connect") or line.strip().startswith("\\c "):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def setup_governance_db():
    print("\n--- 1. Applying Governance Schema ---")
    schema_file = ROOT_DIR / "infra" / "postgres" / "02_governance_schema.sql"
    with open(schema_file, "r") as f:
        sql = sanitize_sql(f.read())

    with psycopg.connect(gov_sync_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            print("Successfully applied 02_governance_schema.sql")

            # Verify tables
            cur.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                ORDER BY table_name;
            """)
            tables = [row[0] for row in cur.fetchall()]
            print(f"Governance DB Tables created: {tables}")


def setup_staging_db():
    print("\n--- 2. Applying Staging Schema & Seeding Data ---")
    seed_file = ROOT_DIR / "infra" / "postgres" / "03_staging_crm_seed.sql"
    with open(seed_file, "r") as f:
        sql = sanitize_sql(f.read())

    with psycopg.connect(staging_sync_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            print("Successfully applied 03_staging_crm_seed.sql")

            # Verify tables and row counts
            cur.execute("SELECT count(*) FROM clients;")
            client_count = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM invoices;")
            invoice_count = cur.fetchone()[0]

            print(f"Staging DB Ready: {client_count} clients seeded, {invoice_count} invoices seeded.")


if __name__ == "__main__":
    try:
        setup_governance_db()
        setup_staging_db()
        print("\nAll database schemas and seed data successfully initialized!")
    except Exception as e:
        print(f"\nMigration failed: {e}", file=sys.stderr)
        sys.exit(1)
