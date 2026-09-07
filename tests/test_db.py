import pytest

from filingcabinet import db


def test_migrate_in_memory_applies_all_and_is_idempotent():
    conn = db.connect(":memory:")
    assert not db.is_migrated(conn)
    applied = db.migrate(conn)
    assert applied and applied[0] == "001_init.sql"
    assert db.is_migrated(conn)
    assert db.migrate(conn) == []
    assert db.pending_migrations(conn) == []


def test_schema_core_tables_present():
    conn = db.connect(":memory:")
    db.migrate(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"document", "occurrence", "move_log", "schema_migrations"} <= names


def test_database_exists_ignores_zero_byte_file(tmp_path):
    p = tmp_path / "x.db"
    p.touch()
    assert not db.database_exists(p)
    conn = db.connect(p)
    db.migrate(conn)
    conn.close()
    assert db.database_exists(p)


def test_require_migrated_raises():
    conn = db.connect(":memory:")
    with pytest.raises(db.NotMigratedError):
        db.require_migrated(conn)


def test_002_adds_occurrence_columns():
    conn = db.connect(":memory:")
    db.migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(occurrence)")}
    assert {"conflict_kind", "hashed_at"} <= cols
