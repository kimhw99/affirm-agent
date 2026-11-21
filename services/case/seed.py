"""Loads the golden cases into case_db.

Reads every case under data/golden (or the folder given), checks the cases with
scripts/validate_golden.py, and loads each case's case.json into the case tables and
its world.json into the mock tables of services/case/schema.sql. It runs schema.sql
first, so the tables exist even before the case service has started. It never loads
expected.json, the case's expected answer: the agent reads case_db through the case
service, so it must not find the answers there.

Each case replaces what was loaded for it before, so running the script again is
safe. It all happens in one transaction: if anything fails, nothing changes. A case
that already has decisions or audit log entries is never replaced, so its history
can't disappear by accident; --reset empties every case_db table first, decisions
and the audit log included.

Usage, from the repo root, once the database is running (docker compose up -d db):

    python services/case/seed.py                    # every case under data/golden
    python services/case/seed.py data/golden/ec1    # one edge case's cases
    python services/case/seed.py --reset            # empty case_db, then load

It connects as case_svc to case_db with CASE_DB_PASSWORD, at CASE_DB_HOST and
CASE_DB_PORT. On this machine they default to localhost and the port docker-compose.yml
publishes (POSTGRES_HOST_PORT, 55432 by default). Settings come from the environment
or from .env in the repo root. It needs the packages in services/case/requirements.txt.

The case service's container has them, and docker-compose.yml points it at the
database, so this works without installing anything:

    docker compose run --rm case python seed.py
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))  # validate_golden.py, which puts contracts/ on the path too

import validate_golden  # noqa: E402
from kyb_contracts.case_input import CaseInput  # noqa: E402
from kyb_contracts.world import World  # noqa: E402

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_PORT = 55432

# One INSERT per table, in an order that satisfies the foreign keys.
INSERTS: dict[str, str] = {
    "cases": """
        INSERT INTO cases (case_id, status, flag_reasons, legal_name, dba, address, tin_type,
                           tin_last4, state, state_entity_id, entity_type, formation_date, checks)
        VALUES (%s, %s, %s::text[], %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
    "case_owners": """
        INSERT INTO case_owners (case_id, owner_ref, name, role)
        VALUES (%s, %s, %s, %s)""",
    "documents": """
        INSERT INTO documents (case_id, doc_id, type, extracted, confidence)
        VALUES (%s, %s, %s, %s::jsonb, %s)""",
    "bank_verifications": """
        INSERT INTO bank_verifications (case_id, bank_id, account_holder, status, verified_at)
        VALUES (%s, %s, %s, %s, %s)""",
    "web_snapshots": """
        INSERT INTO web_snapshots (case_id, web_id, url, captured_at, extracted)
        VALUES (%s, %s, %s, %s, %s::jsonb)""",
    "owner_kyc": """
        INSERT INTO owner_kyc (case_id, kyc_id, owner_ref, result)
        VALUES (%s, %s, %s, %s)""",
    "mock.registry_entities": """
        INSERT INTO mock.registry_entities (state, state_entity_id, legal_name, former_names,
            entity_type, entity_status, formation_date, registered_address, officers, source_case)
        VALUES (%s, %s, %s, %s::text[], %s, %s, %s, %s, %s::jsonb, %s)""",
    "mock.registry_filings": """
        INSERT INTO mock.registry_filings (event_id, state, state_entity_id, event_type,
            effective_date, old_value, new_value)
        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
    "mock.dba_filings": """
        INSERT INTO mock.dba_filings (filing_id, state, county, dba, registrant, registrant_type,
            registrant_state_entity_id, filed, expires, status, source_case)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
    "mock.commercial_profiles": """
        INSERT INTO mock.commercial_profiles (profile_id, state, business_name, address,
            first_seen, last_updated, source_case)
        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
    "mock.tin_records": """
        INSERT INTO mock.tin_records (case_id, tin_type, tin_last4, irs_status, irs_name)
        VALUES (%s, %s, %s, %s, %s)""",
}


def case_rows(case: CaseInput) -> dict[str, list[tuple]]:
    """case.json as rows of the case tables."""
    cid, app, bank = case.case_id, case.application, case.bank_verification
    return {
        "cases": [(cid, case.status.value, [flag.value for flag in case.flag_reasons], app.legal_name,
                   app.dba, app.address, app.tin_type, app.tin_last4, app.state, app.state_entity_id,
                   app.entity_type, app.formation_date,
                   json.dumps(case.checks.model_dump(mode="json", exclude_none=True)))],
        "case_owners": [(cid, o.ref, o.name, o.role) for o in app.owners],
        "documents": [(cid, d.id, d.type, json.dumps(d.extracted), d.confidence) for d in case.documents],
        "bank_verifications": [(cid, bank.id, bank.account_holder, bank.status, bank.verified_at)] if bank else [],
        "web_snapshots": [(cid, w.id, str(w.url), w.captured_at, json.dumps(w.extracted))
                          for w in case.web_snapshot],
        "owner_kyc": [(cid, k.id, k.owner_ref, k.result) for k in case.owner_kyc],
    }


def world_rows(world: World) -> dict[str, list[tuple]]:
    """world.json as rows of the mock tables."""
    cid, tin = world.case_id, world.tin_record
    return {
        "mock.registry_entities": [
            (e.state, e.state_entity_id, e.legal_name, e.former_names, e.entity_type, e.entity_status,
             e.formation_date, e.registered_address,
             json.dumps([o.model_dump(mode="json") for o in e.officers]), cid)
            for e in world.registry],
        "mock.registry_filings": [
            (f.event_id, e.state, e.state_entity_id, f.event_type, f.effective_date, f.old_value, f.new_value)
            for e in world.registry for f in e.filings],
        "mock.dba_filings": [
            (d.filing_id, d.state, d.county, d.dba, d.registrant, d.registrant_type,
             d.registrant_state_entity_id, d.filed, d.expires, d.status, cid)
            for d in world.dba_filings],
        "mock.commercial_profiles": [
            (c.profile_id, c.state, c.business_name, c.address, c.first_seen, c.last_updated, cid)
            for c in world.commercial],
        "mock.tin_records": [(cid, tin.tin_type, tin.tin_last4, tin.irs_status, tin.irs_name)] if tin else [],
    }


def load_env(path: Path = REPO / ".env") -> None:
    """Read KEY=VALUE lines from .env, keeping any variable that is already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key, value.strip())


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Load the golden cases into case_db.")
    parser.add_argument("path", nargs="?", type=Path, default=validate_golden.GOLDEN,
                        help="data/golden (the default), one edge case's folder, or one case folder")
    parser.add_argument("--reset", action="store_true",
                        help="empty every case_db table first, decisions and audit log included")
    parser.add_argument("--host", default=os.environ.get("CASE_DB_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(
        os.environ.get("CASE_DB_PORT") or os.environ.get("POSTGRES_HOST_PORT") or DEFAULT_PORT))
    args = parser.parse_args(argv)

    if not args.path.is_dir():
        print(f"{args.path} is not a folder")
        return 2
    cases, problems = validate_golden.validate(args.path)
    problems = [f"{golden.label}: {p}" for golden in cases for p in golden.problems] + problems
    if problems:
        print("validate_golden.py found problems, so nothing was loaded:")
        print("\n".join(f"  - {problem}" for problem in problems))
        return 1
    password = os.environ.get("CASE_DB_PASSWORD")
    if not password:
        print("CASE_DB_PASSWORD is not set: copy .env.example to .env and fill it in")
        return 2

    import psycopg  # imported here, so --help and the checks above work without it

    try:
        conn = psycopg.connect(host=args.host, port=args.port, dbname="case_db", user="case_svc",
                               password=password, connect_timeout=5)
    except psycopg.OperationalError as error:
        print(f"Can't connect to case_db at {args.host}:{args.port}. Is the database running "
              f"(docker compose up -d db)?\n{error}")
        return 1

    ids = [golden.case.case_id for golden in cases]
    try:
        with conn:  # one transaction: commits at the end, or rolls back on any error
            conn.execute(SCHEMA.read_text(encoding="utf-8"))
            if args.reset:
                conn.execute("TRUNCATE cases CASCADE")
            kept = [row[0] for row in conn.execute(
                "SELECT case_id FROM decisions WHERE case_id = ANY(%s::text[]) "
                "UNION SELECT case_id FROM audit_log WHERE case_id = ANY(%s::text[]) ORDER BY 1",
                [ids, ids])]
            if kept:
                conn.rollback()
                print(f"Nothing was loaded: these cases have decisions or audit log entries, so they "
                      f"can't be replaced: {', '.join(kept)}. Use --reset to empty case_db first.")
                return 1
            conn.execute("DELETE FROM cases WHERE case_id = ANY(%s::text[])", [ids])  # cascades
            with conn.cursor() as cur:
                for golden in cases:
                    rows = case_rows(golden.case) | world_rows(golden.world)
                    for table, insert in INSERTS.items():
                        if rows[table]:
                            cur.executemany(insert, rows[table])
    except psycopg.Error as error:
        print(f"Nothing was loaded:\n{error}")
        return 1

    emptied = "Emptied case_db, then loaded" if args.reset else "Loaded"
    print(f"{emptied} {len(cases)} case(s): {', '.join(g.label for g in cases) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
