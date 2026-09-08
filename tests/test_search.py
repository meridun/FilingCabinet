import pytest

from filingcabinet import db, search

NOW = "2026-01-01T00:00:00.000000+00:00"


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def _add_document(conn, sha: str, text: str | None, *, rel_path: str | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, mime, page_count, ocr_text, first_seen_at, "
        "updated_at) VALUES (?, 1, 'application/pdf', 2, ?, ?, ?)",
        (sha, text, NOW, NOW),
    )
    document_id = int(cursor.lastrowid)
    if rel_path is not None:
        conn.execute(
            "INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at) "
            "VALUES (?, ?, 0.0, 1, ?)",
            (document_id, rel_path, NOW),
        )
    return document_id


def test_escape_query_wraps_bare_text_and_keeps_operators():
    assert search.escape_query("northwind") == '"northwind"'
    assert search.escape_query("  o'brien  ") == '"o\'brien"'
    assert search.escape_query('say "hi"') == 'say "hi"'
    assert search.escape_query("invoice OR receipt") == "invoice OR receipt"
    assert search.escape_query("north*") == "north*"
    with pytest.raises(ValueError):
        search.escape_query("   ")


def test_find_returns_matching_documents(conn):
    wanted = _add_document(conn, "sha-a", "Northwind invoice for office chairs", rel_path="a.pdf")
    _add_document(conn, "sha-b", "Council tax statement", rel_path="b.pdf")

    hits = search.find(conn, "northwind")
    assert [hit.document_id for hit in hits] == [wanted]
    hit = hits[0]
    assert hit.rel_path == "a.pdf" and hit.sha256 == "sha-a" and hit.page_count == 2
    assert "[Northwind]" in hit.snippet


def test_find_ranks_and_limits(conn):
    _add_document(conn, "sha-1", "invoice", rel_path="one.pdf")
    _add_document(conn, "sha-2", "invoice invoice invoice", rel_path="two.pdf")
    _add_document(conn, "sha-3", "invoice invoice", rel_path="three.pdf")

    hits = search.find(conn, "invoice")
    assert [hit.rel_path for hit in hits] == ["two.pdf", "three.pdf", "one.pdf"]
    assert search.find(conn, "invoice", limit=1) == hits[:1]


def test_find_ignores_documents_without_text(conn):
    _add_document(conn, "sha-empty", None, rel_path="empty.pdf")
    assert search.find(conn, "anything") == []


def test_find_query_with_punctuation_does_not_raise(conn):
    _add_document(conn, "sha-p", "O'Brien & Sons - invoice 2026/14", rel_path="p.pdf")
    for query in ["o'brien", "invoice 2026/14", "sons - invoice", "((("]:
        assert isinstance(search.find(conn, query), list)


def test_find_reports_a_malformed_expression_as_value_error(conn):
    _add_document(conn, "sha-m", "text", rel_path="m.pdf")
    with pytest.raises(ValueError):
        search.find(conn, 'unclosed "quote AND')


def test_find_reports_a_missing_document_path(conn):
    _add_document(conn, "sha-none", "orphan text")  # indexed, no live occurrence
    hits = search.find(conn, "orphan")
    assert len(hits) == 1 and hits[0].rel_path is None


def test_find_requires_migrated_db():
    conn = db.connect(":memory:")
    with pytest.raises(db.NotMigratedError):
        search.find(conn, "anything")
    conn.close()
