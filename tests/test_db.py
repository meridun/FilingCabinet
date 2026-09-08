import sqlite3

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


def test_003_adds_scan_table_and_occurrence_column():
    conn = db.connect(":memory:")
    db.migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(occurrence)")}
    assert "last_scan_id" in cols
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scan" in tables


def test_005_adds_page_ocr_and_fts():
    conn = db.connect(":memory:")
    db.migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(page_ocr)")}
    assert {
        "document_id",
        "page_number",
        "text",
        "confidence",
        "rung",
        "ocr_source",
        "status",
        "note",
        "updated_at",
    } <= cols
    objects = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "document_fts" in objects  # FTS5 is required here, not optional


def _insert_document(conn, sha, text):
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, ocr_text, first_seen_at, updated_at) "
        "VALUES (?, 1, ?, 'now', 'now')",
        (sha, text),
    )
    return int(cursor.lastrowid)


def _fts_matches(conn, term):
    return [
        row["rowid"]
        for row in conn.execute(
            "SELECT rowid FROM document_fts WHERE document_fts MATCH ?", (term,)
        )
    ]


def test_005_fts_triggers_track_ocr_text():
    conn = db.connect(":memory:")
    db.migrate(conn)
    document_id = _insert_document(conn, "sha-1", "alpha beta")
    assert _fts_matches(conn, "alpha") == [document_id]

    conn.execute("UPDATE document SET ocr_text = ? WHERE document_id = ?", ("gamma", document_id))
    assert _fts_matches(conn, "alpha") == []
    assert _fts_matches(conn, "gamma") == [document_id]

    conn.execute("DELETE FROM document WHERE document_id = ?", (document_id,))
    assert _fts_matches(conn, "gamma") == []


def test_005_backfills_documents_written_before_the_migration(tmp_path):
    """A database migrated only as far as 004 still lands in the index when 005 applies."""
    older = tmp_path / "migrations"
    older.mkdir()
    for path in sorted(db.DEFAULT_MIGRATIONS_DIR.glob("*.sql")):
        if path.name.startswith("005"):
            continue
        (older / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    conn = db.connect(":memory:")
    db.migrate(conn, older)
    document_id = _insert_document(conn, "sha-old", "predates the index")
    db.migrate(conn)  # apply 005 on top of an already-populated database
    assert _fts_matches(conn, "predates") == [document_id]


def test_006_adds_classification_table():
    conn = db.connect(":memory:")
    db.migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(classification)")}
    assert {
        "document_id",
        "party",
        "doc_type",
        "detail",
        "doc_date",
        "tags",
        "folder",
        "provenance",
        "note",
        "created_at",
        "updated_at",
    } <= cols


def test_006_classification_is_one_row_per_document():
    conn = db.connect(":memory:")
    db.migrate(conn)
    document_id = _insert_document(conn, "sha-c", "text")
    conn.execute(
        "INSERT INTO classification (document_id, provenance, created_at, updated_at) "
        "VALUES (?, 'agent', 'now', 'now')",
        (document_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO classification (document_id, provenance, created_at, updated_at) "
            "VALUES (?, 'agent', 'now', 'now')",
            (document_id,),
        )


def test_migrate_is_still_a_no_op_on_a_fully_migrated_database(tmp_path):
    """006 is additive: re-running `migrate` applies nothing and disturbs nothing."""
    conn = db.connect(tmp_path / "fc.db")
    applied = db.migrate(conn)
    assert "006_organize.sql" in applied
    assert db.migrate(conn) == []
