"""app/db.py's ORM models agree with schema.sql, which creates the tables: the same
tables, columns, types, nullability, primary keys and foreign keys.

schema.sql stays the one source of the tables, so a change there must be made in
app/db.py too; this test says what no longer matches. No database is needed.
"""

import re
from pathlib import Path

from sqlalchemy.dialects import postgresql

from app import db

SCHEMA = Path(__file__).resolve().parents[1] / "schema.sql"

# How schema.sql writes each type, and how SQLAlchemy compiles the model's type.
TYPES = {"double precision": "DOUBLE PRECISION", "timestamptz": "TIMESTAMP WITH TIME ZONE", "text[]": "TEXT[]",
         "text": "TEXT", "jsonb": "JSONB", "date": "DATE", "boolean": "BOOLEAN", "bigint": "BIGINT"}


def schema_tables(sql: str) -> dict[str, dict]:
    """Each CREATE TABLE in sql: {column: (type, nullable)}, the primary key, and the foreign keys."""
    tables = {}
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS ([\w.]+) \((.*?)\n\);", sql, re.S):
        columns, primary_key, foreign_keys = {}, set(), set()
        for line in body.splitlines():
            line = line.split("--")[0].strip().rstrip(",")
            if not line or line.startswith(("(", "UNIQUE", "CHECK")):
                continue                     # a check's second line, or a constraint this test leaves out
            if line.startswith("PRIMARY KEY"):
                primary_key |= set(re.findall(r"\w+", line.removeprefix("PRIMARY KEY")))
                continue
            if line.startswith("FOREIGN KEY"):
                fk_columns, target = re.match(r"FOREIGN KEY \(([^)]*)\) REFERENCES ([\w.]+)", line).groups()
                foreign_keys |= {(column.strip(), target) for column in fk_columns.split(",")}
                continue
            column, rest = line.split(None, 1)
            type_ = next(TYPES[t] for t in TYPES if rest.startswith(t))
            columns[column] = (type_, "NOT NULL" not in rest and "PRIMARY KEY" not in rest)
            if "PRIMARY KEY" in rest:
                primary_key.add(column)
            if reference := re.search(r"REFERENCES ([\w.]+)", rest):
                foreign_keys.add((column, reference.group(1)))
        for column in primary_key:
            columns[column] = (columns[column][0], False)
        tables[name] = {"columns": columns, "primary_key": primary_key, "foreign_keys": foreign_keys}
    return tables


def model_tables() -> dict[str, dict]:
    """The same, from the ORM models."""
    dialect = postgresql.dialect()
    return {table.fullname: {
        "columns": {c.name: (c.type.compile(dialect=dialect), c.nullable) for c in table.columns},
        "primary_key": {c.name for c in table.primary_key.columns},
        "foreign_keys": {(fk.parent.name, fk.column.table.fullname) for fk in table.foreign_keys},
    } for table in db.Base.metadata.sorted_tables}


def test_every_table_in_schema_sql_has_a_model_and_the_other_way_round():
    assert sorted(model_tables()) == sorted(schema_tables(SCHEMA.read_text(encoding="utf-8")))


def test_each_model_matches_its_table():
    schema, models = schema_tables(SCHEMA.read_text(encoding="utf-8")), model_tables()
    for name, table in schema.items():
        assert models[name] == table, name


def test_foreign_keys_point_at_the_primary_key():
    for table in db.Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            assert fk.column.primary_key, f"{table.fullname}.{fk.parent.name}"


def test_the_parser_reads_schema_sql():
    tables = schema_tables(SCHEMA.read_text(encoding="utf-8"))
    assert len(tables) == 13
    assert tables["cases"]["columns"]["status"] == ("TEXT", False)
    assert tables["cases"]["columns"]["dba"] == ("TEXT", True)
    assert tables["mock.registry_filings"]["foreign_keys"] == {("state", "mock.registry_entities"),
                                                                ("state_entity_id", "mock.registry_entities")}
    assert tables["audit_log"]["columns"]["id"] == ("BIGINT", False)
