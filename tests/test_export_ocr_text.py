import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from filingcabinet import db

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-01-01T00:00:00.000000+00:00"


def _load(name: str):
    """Load a script from scripts/ by path: they are deliberately not part of the package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export_ocr_text = _load("export_ocr_text")


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


def _add_document(
    conn,
    sha: str,
    text: str | None,
    *,
    rel_path: str | None = None,
    party: str | None = None,
    doc_date: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, mime, page_count, doc_date, party, ocr_text, "
        "first_seen_at, updated_at) VALUES (?, 1, 'application/pdf', 2, ?, ?, ?, ?, ?)",
        (sha, doc_date, party, text, NOW, NOW),
    )
    document_id = int(cursor.lastrowid)
    if rel_path is not None:
        conn.execute(
            "INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at) "
            "VALUES (?, ?, 0.0, 1, ?)",
            (document_id, rel_path, NOW),
        )
    return document_id


def _add_page(conn, document_id: int, page_number: int, text: str, *, status: str = "ok") -> None:
    conn.execute(
        "INSERT INTO page_ocr (document_id, page_number, text, confidence, rung, ocr_source, "
        "status, updated_at) VALUES (?, ?, ?, 0.9, 'local_text', 'local_text', ?, ?)",
        (document_id, page_number, text, status, NOW),
    )


def test_export_writes_one_file_per_document(conn, tmp_path):
    _add_document(conn, "sha-a", "Northwind invoice", rel_path="a.pdf")
    _add_document(conn, "sha-b", None, rel_path="b.pdf")
    _add_document(conn, "sha-c", "   \n  ", rel_path="c.pdf")

    exported = export_ocr_text.export(conn, tmp_path / "out")

    files = sorted(p.name for p in (tmp_path / "out").glob("*.md"))
    assert len(files) == 1 and len(exported) == 1
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["document_count"] == 1
    assert manifest["documents"][0]["out_file"] == files[0]
    assert "ocr_text" not in json.dumps(manifest) and "Northwind" not in json.dumps(manifest)


def test_export_renders_page_sections_when_page_ocr_rows_exist(conn, tmp_path):
    paged = _add_document(conn, "sha-paged", "page one\n\npage two", rel_path="paged.pdf")
    _add_page(conn, paged, 1, "page one")
    _add_page(conn, paged, 2, "page two")
    _add_document(conn, "sha-plain", "rolled up text only", rel_path="plain.pdf")

    exported = {doc.document_id: doc for doc in export_ocr_text.export(conn, tmp_path / "out")}

    paged_text = (tmp_path / "out" / exported[paged].out_file).read_text(encoding="utf-8")
    assert "## Page 1" in paged_text and "## Page 2" in paged_text and "## Text" not in paged_text
    plain_id = next(doc_id for doc_id in exported if doc_id != paged)
    plain_text = (tmp_path / "out" / exported[plain_id].out_file).read_text(encoding="utf-8")
    assert "## Text" in plain_text and "rolled up text only" in plain_text


def test_export_skips_page_rows_that_are_not_ok(conn, tmp_path):
    doc = _add_document(conn, "sha-mixed", "good page", rel_path="mixed.pdf")
    _add_page(conn, doc, 1, "good page")
    _add_page(conn, doc, 2, None, status="pending_vision")

    exported = export_ocr_text.export(conn, tmp_path / "out")

    body = (tmp_path / "out" / exported[0].out_file).read_text(encoding="utf-8")
    assert "## Page 1" in body and "## Page 2" not in body


def test_front_matter_carries_identity_and_filename_is_content_free(conn, tmp_path):
    doc = _add_document(
        conn,
        "abcdef0123456789",
        "text",
        rel_path="scans/Marlowe-tax-return-2024.pdf",
        party="northwind-utilities",
        doc_date="2025-01-14",
    )

    exported = export_ocr_text.export(conn, tmp_path / "out")

    assert re.fullmatch(r"\d{5}-[0-9a-f]{12}\.md", exported[0].out_file)
    assert "Marlowe" not in exported[0].out_file and "tax" not in exported[0].out_file
    body = (tmp_path / "out" / exported[0].out_file).read_text(encoding="utf-8")
    assert f"document_id: {doc}" in body
    assert "sha256: abcdef0123456789" in body
    assert "doc_date: 2025-01-14" in body
    assert "party: northwind-utilities" in body


def test_export_limit_is_deterministic(conn, tmp_path):
    first = _add_document(conn, "sha-1", "one", rel_path="1.pdf")
    second = _add_document(conn, "sha-2", "two", rel_path="2.pdf")
    _add_document(conn, "sha-3", "three", rel_path="3.pdf")

    exported = export_ocr_text.export(conn, tmp_path / "out", limit=2)

    assert [doc.document_id for doc in exported] == [first, second]


def test_export_refuses_out_dir_inside_the_repo(conn, tmp_path):
    target = ROOT / "exports" / "graphify-spike"

    with pytest.raises(SystemExit) as excinfo:
        export_ocr_text.export(conn, target)

    assert excinfo.value.code == 2
    assert not target.exists()


def test_main_refuses_missing_database(tmp_path):
    with pytest.raises(SystemExit):
        export_ocr_text.main(["--db", str(tmp_path / "absent.db"), "--out", str(tmp_path / "out")])
