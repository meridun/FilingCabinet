import importlib.util
import sys
from pathlib import Path

import pytest

from filingcabinet import db, search

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    """Load a script from scripts/ by path: they are deliberately not part of the package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


make_demo_corpus = _load("make_demo_corpus")


def test_demo_corpus_builds_a_migrated_index(tmp_path):
    summary = make_demo_corpus.build(tmp_path / "demo.db", documents=6)

    assert summary["document_count"] == 6
    conn = db.connect(tmp_path / "demo.db")
    try:
        assert db.is_migrated(conn)
        assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM occurrence").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM page_ocr").fetchone()[0] == 12
    finally:
        conn.close()


def test_demo_corpus_has_cross_document_entities(tmp_path):
    make_demo_corpus.build(tmp_path / "demo.db")

    conn = db.connect(tmp_path / "demo.db")
    try:
        for vendor in make_demo_corpus.VENDORS:
            assert len({hit.document_id for hit in search.find(conn, vendor)}) >= 2, vendor
        for person in make_demo_corpus.PEOPLE:
            assert len({hit.document_id for hit in search.find(conn, person)}) >= 2, person
        for account in make_demo_corpus.ACCOUNTS:
            assert len({hit.document_id for hit in search.find(conn, account)}) >= 2, account
        assert len(search.find(conn, "Zephyr Bicycle Repair")) == 1
        assert len(search.find(conn, "Marrowfield Removals")) == 1
    finally:
        conn.close()


def test_demo_corpus_pads_beyond_the_scripted_documents(tmp_path):
    summary = make_demo_corpus.build(tmp_path / "demo.db", documents=len(make_demo_corpus.SPECS) + 2)

    assert summary["document_count"] == len(make_demo_corpus.SPECS) + 2
    conn = db.connect(tmp_path / "demo.db")
    try:
        assert len(search.find(conn, "Fillerton")) == 2
    finally:
        conn.close()


def test_demo_corpus_refuses_db_inside_the_repo():
    target = ROOT / "demo-corpus.db"

    with pytest.raises(SystemExit) as excinfo:
        make_demo_corpus.build(target)

    assert excinfo.value.code == 2
    assert not target.exists()
