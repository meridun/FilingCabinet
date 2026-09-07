import itertools
import os
import tracemalloc
from collections.abc import Iterator

import pytest

from filingcabinet import db, dedup

NOW = "2026-01-01T00:00:00.000000+00:00"


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture()
def available(monkeypatch):
    """Force the optional image stack "present" - the logic tests never render a page."""
    monkeypatch.setattr(dedup, "phash_available", lambda: True)


def _hash(value: int) -> str:
    """A 16-hex-character page hash, the width PHASH_ALGO produces."""
    return f"{value:016x}"


def _add_document(conn, sha: str, *, page_count: int | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, page_count, first_seen_at, updated_at) "
        "VALUES (?, 1, ?, ?, ?)",
        (sha, page_count, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _add_occurrence(conn, document_id: int, rel_path: str, *, missing: bool = False) -> None:
    conn.execute(
        "INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at, "
        "missing_since) VALUES (?, ?, 0.0, 1, ?, ?)",
        (document_id, rel_path, NOW, NOW if missing else None),
    )


def _add_page_hashes(conn, document_id: int, hashes: list[str]) -> None:
    conn.executemany(
        "INSERT INTO page_hash (document_id, page_no, phash, algo, computed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(document_id, i, h, dedup.PHASH_ALGO, NOW) for i, h in enumerate(hashes)],
    )


def _doc_with_pages(conn, sha: str, hashes: list[str]) -> int:
    document_id = _add_document(conn, sha, page_count=len(hashes))
    _add_occurrence(conn, document_id, f"{sha}.pdf")
    _add_page_hashes(conn, document_id, hashes)
    return document_id


# --- pure helpers -------------------------------------------------------------------


def test_hamming_hex_identical_is_zero():
    assert dedup.hamming_hex(_hash(0xABCD), _hash(0xABCD)) == 0


def test_hamming_hex_counts_single_bit():
    assert dedup.hamming_hex(_hash(0b0), _hash(0b1)) == 1
    assert dedup.hamming_hex(_hash(0b0), _hash(0b1011)) == 3


def test_hamming_hex_rejects_width_mismatch():
    with pytest.raises(ValueError):
        dedup.hamming_hex("abcd", _hash(1))


def test_match_pages_requires_a_no_longer_than_b():
    assert dedup.match_pages([_hash(1), _hash(2)], [_hash(1)], max_distance=8) is None
    assert dedup.match_pages([], [_hash(1)], max_distance=8) is None


def test_match_pages_uses_each_b_page_once():
    # Both A pages look like the one identical B page; the second finds no free partner.
    pages_a = [_hash(1), _hash(1)]
    pages_b = [_hash(1), _hash(0xFF00)]
    assert dedup.match_pages(pages_a, pages_b, max_distance=1) is None


# --- tier 1: exact ------------------------------------------------------------------


def test_exact_duplicates_groups_multiple_live_paths(conn):
    document_id = _add_document(conn, "sha-a")
    _add_occurrence(conn, document_id, "one/a.pdf")
    _add_occurrence(conn, document_id, "two/a.pdf")
    assert dedup.exact_duplicates(conn) == [(document_id, ["one/a.pdf", "two/a.pdf"])]


def test_exact_duplicates_ignores_singletons(conn):
    document_id = _add_document(conn, "sha-a")
    _add_occurrence(conn, document_id, "a.pdf")
    assert dedup.exact_duplicates(conn) == []


def test_exact_duplicates_ignores_missing_occurrences(conn):
    document_id = _add_document(conn, "sha-a")
    _add_occurrence(conn, document_id, "a.pdf")
    _add_occurrence(conn, document_id, "gone/a.pdf", missing=True)
    assert dedup.exact_duplicates(conn) == []


# --- tier 2: near -------------------------------------------------------------------


def test_near_duplicates_matches_within_threshold(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(0b0000), _hash(0b1000)])
    b = _doc_with_pages(conn, "sha-b", [_hash(0b0001), _hash(0b1001)])
    matches = dedup.near_duplicates(conn, max_distance=1)
    assert [(m.kind, m.document_a, m.document_b) for m in matches] == [("near", a, b)]
    assert matches[0].score == 1.0


def test_near_duplicates_rejects_outside_threshold(conn):
    _doc_with_pages(conn, "sha-a", [_hash(0b0000)])
    _doc_with_pages(conn, "sha-b", [_hash(0b1111)])
    assert dedup.near_duplicates(conn, max_distance=3) == []


def test_near_duplicates_threshold_zero_admits_only_identical(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(7)])
    b = _doc_with_pages(conn, "sha-b", [_hash(7)])
    _doc_with_pages(conn, "sha-c", [_hash(6)])
    matches = dedup.near_duplicates(conn, max_distance=0)
    assert [(m.document_a, m.document_b) for m in matches] == [(a, b)]


def test_near_duplicates_ignores_unequal_page_counts(conn):
    _doc_with_pages(conn, "sha-a", [_hash(1)])
    _doc_with_pages(conn, "sha-b", [_hash(1), _hash(2)])
    assert dedup.near_duplicates(conn, max_distance=2) == []


def test_candidate_pairs_emits_shared_hash_candidates_first():
    pages = {1: [_hash(1)], 2: [_hash(9)], 3: [_hash(1)]}
    pairs = list(dedup._candidate_pairs(pages, equal_length=True))
    assert pairs[0] == (1, 3)  # the pair sharing an exact page hash leads
    assert sorted(pairs) == [(1, 2), (1, 3), (2, 3)]


def test_candidate_pairs_never_materializes_the_cross_product():
    """A caller that stops at its budget must not pay for the whole corpus squared."""
    pages = {doc_id: [_hash(doc_id)] for doc_id in range(4000)}  # ~8M pairs if listed
    pairs = dedup._candidate_pairs(pages, equal_length=True)
    assert isinstance(pairs, Iterator)  # lazy by contract, never a list
    tracemalloc.start()
    try:
        taken = list(itertools.islice(pairs, 100))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(set(taken)) == 100
    assert peak < 5_000_000  # materializing 8M pairs cost ~1 GB


def test_near_duplicates_stops_at_the_pair_budget_and_says_so(conn):
    for index in range(4):  # 6 candidate pairs, all matching
        _doc_with_pages(conn, f"sha-{index}", [_hash(1)])
    stats = dedup.ScanStats()
    matches = dedup.near_duplicates(conn, max_distance=0, max_pairs=2, stats=stats)
    assert len(matches) == 2
    assert stats.truncated is True


def test_near_duplicates_within_the_budget_is_not_truncated(conn):
    _doc_with_pages(conn, "sha-a", [_hash(1)])
    _doc_with_pages(conn, "sha-b", [_hash(1)])
    stats = dedup.ScanStats()
    assert len(dedup.near_duplicates(conn, max_distance=0, max_pairs=10, stats=stats)) == 1
    assert stats.truncated is False


def test_subset_matches_stops_at_the_pair_budget_and_says_so(conn):
    _doc_with_pages(conn, "sha-a", [_hash(1)])
    _doc_with_pages(conn, "sha-b", [_hash(1)])
    _doc_with_pages(conn, "sha-c", [_hash(1), _hash(1)])
    stats = dedup.ScanStats()
    matches = dedup.subset_matches(conn, max_distance=0, max_pairs=1, stats=stats)
    assert len(matches) == 1
    assert stats.truncated is True


# --- tier 3: subset -----------------------------------------------------------------


def test_subset_matches_finds_contained_document(conn):
    small = _doc_with_pages(conn, "sha-a", [_hash(1), _hash(2), _hash(3)])
    large = _doc_with_pages(conn, "sha-b", [_hash(i) for i in range(10)])
    matches = dedup.subset_matches(conn, max_distance=0)
    assert [(m.kind, m.document_a, m.document_b) for m in matches] == [("subset", small, large)]
    assert matches[0].score == pytest.approx(0.3)


def test_subset_matches_rejects_foreign_page(conn):
    _doc_with_pages(conn, "sha-a", [_hash(1), _hash(2), _hash(0xFFFFFFFFFFFFFFFF)])
    _doc_with_pages(conn, "sha-b", [_hash(i) for i in range(10)])
    assert dedup.subset_matches(conn, max_distance=1) == []


def test_subset_matches_ignores_equal_page_counts(conn):
    _doc_with_pages(conn, "sha-a", [_hash(1), _hash(2)])
    _doc_with_pages(conn, "sha-b", [_hash(1), _hash(2)])
    assert dedup.subset_matches(conn, max_distance=0) == []


# --- review queue -------------------------------------------------------------------


def _match(kind, a, b):
    return dedup.Match(kind=kind, document_a=a, document_b=b, score=1.0, detail="d")


def test_queue_review_is_idempotent(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    assert dedup.queue_review(conn, [_match("near", a, b)], now=NOW) == 1
    assert dedup.queue_review(conn, [_match("near", a, b)], now=NOW) == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM dupe_review").fetchone()["n"] == 1


def test_queue_review_refreshes_score_and_detail(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    dedup.queue_review(conn, [_match("near", a, b)], now=NOW)
    refreshed = dedup.Match(kind="near", document_a=a, document_b=b, score=0.5, detail="fresh")
    assert dedup.queue_review(conn, [refreshed], now=NOW) == 0
    row = conn.execute("SELECT score, detail FROM dupe_review").fetchone()
    assert (row["score"], row["detail"]) == (0.5, "fresh")


def test_queue_review_preserves_a_human_verdict(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    dedup.queue_review(conn, [_match("near", a, b)], now=NOW)
    with conn:
        conn.execute("UPDATE dupe_review SET status = 'dup', resolved_at = ?", (NOW,))
    dedup.queue_review(conn, [_match("near", a, b)], now=NOW)
    row = conn.execute("SELECT status, resolved_at FROM dupe_review").fetchone()
    assert row["status"] == "dup" and row["resolved_at"] == NOW


def test_queue_review_normalizes_near_pairs_only(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    dedup.queue_review(conn, [_match("near", b, a), _match("subset", b, a)], now=NOW)
    rows = {
        row["kind"]: (row["document_a"], row["document_b"])
        for row in conn.execute("SELECT kind, document_a, document_b FROM dupe_review")
    }
    assert rows["near"] == (a, b)  # normalized
    assert rows["subset"] == (b, a)  # order is meaningful: A is contained in B


def test_pending_reviews_lists_only_unresolved(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    c = _doc_with_pages(conn, "sha-c", [_hash(1)])
    dedup.queue_review(conn, [_match("near", a, b), _match("near", a, c)], now=NOW)
    dedup.record_label(conn, a, b, "near", "dup", now=NOW)
    assert [(r["document_a"], r["document_b"]) for r in dedup.pending_reviews(conn)] == [(a, c)]


# --- labelled sample ----------------------------------------------------------------


def test_record_label_updates_rather_than_duplicates(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    assert dedup.record_label(conn, a, b, "near", "not-dup", now=NOW) == "not_dup"
    assert dedup.record_label(conn, a, b, "near", "dup", now=NOW) == "dup"
    labels = dedup.export_labels(conn)
    assert len(labels) == 1 and labels[0]["verdict"] == "dup"


def test_record_label_resolves_the_review_row(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    dedup.queue_review(conn, [_match("near", a, b)], now=NOW)
    dedup.record_label(conn, b, a, "near", "dup", now=NOW)  # order normalized on the way in
    row = conn.execute("SELECT status, resolved_at FROM dupe_review").fetchone()
    assert row["status"] == "dup" and row["resolved_at"] == NOW


def test_record_label_rejects_bad_kind_or_verdict(conn):
    a = _doc_with_pages(conn, "sha-a", [_hash(1)])
    b = _doc_with_pages(conn, "sha-b", [_hash(1)])
    with pytest.raises(ValueError):
        dedup.record_label(conn, a, b, "sideways", "dup", now=NOW)
    with pytest.raises(ValueError):
        dedup.record_label(conn, a, b, "near", "maybe", now=NOW)


# --- run_report ---------------------------------------------------------------------


def _tree_state(root):
    state = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            stat = os.stat(path)
            state[path] = (stat.st_size, stat.st_mtime)
    return state


def test_run_report_counts_every_tier(conn, tmp_path, available):
    root = tmp_path / "docs"
    root.mkdir()
    exact = _add_document(conn, "sha-x")
    _add_occurrence(conn, exact, "one/x.pdf")
    _add_occurrence(conn, exact, "two/x.pdf")
    _doc_with_pages(conn, "sha-a", [_hash(0b00), _hash(0b10)])
    _doc_with_pages(conn, "sha-b", [_hash(0b01), _hash(0b11)])
    _doc_with_pages(conn, "sha-c", [_hash(0b00), _hash(0b10), _hash(0xF0)])

    summary = dedup.run_report(conn, root, max_distance=1, now=NOW)
    assert (summary.exact_groups, summary.exact_documents) == (1, 2)
    assert summary.near == 1
    assert summary.subset == 2  # a and b each sit inside c
    assert summary.queued == 3
    assert summary.phash_available is True and summary.truncated is False


def test_run_report_is_idempotent(conn, tmp_path, available):
    root = tmp_path / "docs"
    root.mkdir()
    _doc_with_pages(conn, "sha-a", [_hash(0b00)])
    _doc_with_pages(conn, "sha-b", [_hash(0b01)])
    first = dedup.run_report(conn, root, max_distance=1, now=NOW)
    second = dedup.run_report(conn, root, max_distance=1, now=NOW)
    assert first.queued == 1 and second.queued == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM dupe_review").fetchone()["n"] == 1


def test_run_report_degrades_without_the_image_stack(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(dedup, "_imagehash", None)
    monkeypatch.setattr(dedup, "_Image", None)
    root = tmp_path / "docs"
    root.mkdir()
    document_id = _add_document(conn, "sha-a", page_count=2)
    _add_occurrence(conn, document_id, "a.pdf")

    summary = dedup.run_report(conn, root, max_distance=6, now=NOW)
    assert summary.phash_available is False
    assert (summary.near, summary.subset, summary.queued, summary.errors) == (0, 0, 0, 0)
    assert summary.hashed_documents == 0


def test_run_report_never_touches_the_document_tree(conn, tmp_path, available):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"alpha")
    (root / "sub" / "b.pdf").write_bytes(b"beta")
    # page_count set, so the backfill really opens a.pdf; it must still only read.
    document_id = _add_document(conn, "sha-a", page_count=1)
    _add_occurrence(conn, document_id, "a.pdf")
    _add_occurrence(conn, document_id, "sub/b.pdf")

    before = _tree_state(root)
    dedup.run_report(conn, root, max_distance=6, now=NOW)
    assert _tree_state(root) == before


def test_run_report_requires_a_migrated_database(tmp_path):
    conn = db.connect(":memory:")
    try:
        with pytest.raises(db.NotMigratedError):
            dedup.run_report(conn, tmp_path, max_distance=6, now=NOW)
    finally:
        conn.close()


def test_ensure_page_hashes_skips_documents_it_cannot_read(conn, tmp_path, available):
    document_id = _add_document(conn, "sha-a", page_count=2)
    _add_occurrence(conn, document_id, "missing.pdf")
    assert dedup.ensure_page_hashes(conn, tmp_path, now=NOW) == (0, 0)
    summary = dedup.run_report(conn, tmp_path, max_distance=6, now=NOW)
    assert summary.errors == 1 and summary.hashed_documents == 0


def test_ensure_page_hashes_refuses_a_path_outside_the_root(conn, tmp_path, available,
                                                            monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    (tmp_path / "outside.pdf").write_bytes(b"%PDF-1.4\n")
    document_id = _add_document(conn, "sha-a", page_count=1)
    _add_occurrence(conn, document_id, os.path.join("..", "outside.pdf"))
    monkeypatch.setattr(
        dedup, "page_phashes", lambda *a, **k: pytest.fail("rendered a file outside the root")
    )
    assert dedup.ensure_page_hashes(conn, root, now=NOW) == (0, 0)


# The real render path needs the optional `dedup` extra; it is skipped where it is absent
# (the degradation test above covers that case).
requires_image_stack = pytest.mark.skipif(
    not dedup.phash_available(), reason="optional `dedup` extra (PyMuPDF, ImageHash, Pillow)"
)


def _write_pdf(path, lines):
    import fitz

    doc = fitz.open()
    for line in lines:
        page = doc.new_page()
        page.insert_text((72, 100), line)
    doc.save(path)
    doc.close()


@requires_image_stack
def test_page_phashes_are_stable_and_page_shaped(tmp_path):
    first = tmp_path / "one.pdf"
    second = tmp_path / "two.pdf"
    _write_pdf(first, ["alpha", "beta"])
    _write_pdf(second, ["alpha", "beta"])
    hashes = dedup.page_phashes(first)
    assert len(hashes) == 2
    assert all(len(h) == 16 and int(h, 16) >= 0 for h in hashes)
    assert dedup.page_phashes(second) == hashes


@requires_image_stack
def test_ensure_page_hashes_backfills_from_the_tree(conn, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    _write_pdf(root / "a.pdf", ["alpha", "beta"])
    document_id = _add_document(conn, "sha-a", page_count=2)
    _add_occurrence(conn, document_id, "a.pdf")

    assert dedup.ensure_page_hashes(conn, root, now=NOW) == (1, 2)
    stored = conn.execute(
        "SELECT COUNT(*) AS n FROM page_hash WHERE document_id = ? AND algo = ?",
        (document_id, dedup.PHASH_ALGO),
    ).fetchone()["n"]
    assert stored == 2
    assert dedup.ensure_page_hashes(conn, root, now=NOW) == (0, 0)  # already hashed
