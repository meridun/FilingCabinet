import hashlib
import os

import pytest

from filingcabinet import db, ingest


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "docs"
    (r / "sub").mkdir(parents=True)
    (r / "a.pdf").write_bytes(b"alpha")
    (r / "b.pdf").write_bytes(b"beta")
    (r / "sub" / "c.png").write_bytes(b"gamma")
    return r


# seen_at and last_scan_id are per-run bookkeeping, not index state.
def _rows(conn, table, skip=("seen_at", "last_scan_id")):
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    return [{k: v for k, v in row.items() if k not in skip} for row in rows]


def _tree_state(root):
    state = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            with open(p, "rb") as fh:
                state[p] = (st.st_size, st.st_mtime, hashlib.sha256(fh.read()).hexdigest())
    return state


@pytest.mark.parametrize(
    "name,expected",
    [
        ("foo (1).pdf", "drive_numbered"),
        ("budget (12).pdf", "drive_numbered"),
        ("notes - Michael's conflicted copy 2024-01-02.pdf", "drive_conflicted_copy"),
        ("Report (final).pdf", None),
        ("invoice.pdf", None),
    ],
)
def test_classify_conflict_patterns(name, expected):
    assert ingest.classify_conflict(name) == expected


def test_first_run_indexes_documents(conn, root):
    summary = ingest.run_ingest(conn, root)
    assert (summary.scanned, summary.new, summary.changed, summary.missing) == (3, 3, 0, 0)
    assert conn.execute("SELECT COUNT(*) n FROM document").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) n FROM occurrence").fetchone()["n"] == 3
    sha = conn.execute(
        "SELECT d.sha256 s FROM document d JOIN occurrence o USING (document_id) "
        "WHERE o.rel_path = 'a.pdf'"
    ).fetchone()["s"]
    assert sha == hashlib.sha256(b"alpha").hexdigest()
    assert conn.execute(
        "SELECT rel_path FROM occurrence WHERE rel_path = 'sub/c.png'"
    ).fetchone() is not None


def test_identical_content_two_paths_share_document(conn, root):
    (root / "a-copy (1).pdf").write_bytes(b"alpha")
    ingest.run_ingest(conn, root)
    assert conn.execute("SELECT COUNT(*) n FROM document").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) n FROM occurrence").fetchone()["n"] == 4
    row = conn.execute(
        "SELECT conflict_kind FROM occurrence WHERE rel_path = 'a-copy (1).pdf'"
    ).fetchone()
    assert row["conflict_kind"] == "drive_numbered"


def test_rerun_skips_hashing_unchanged(conn, root, monkeypatch):
    ingest.run_ingest(conn, root)
    calls = []
    real = ingest.sha256_file
    monkeypatch.setattr(
        ingest, "sha256_file", lambda p, *a, **k: (calls.append(p), real(p))[1]
    )
    summary = ingest.run_ingest(conn, root)
    assert calls == []
    assert (summary.new, summary.changed, summary.missing, summary.unchanged) == (0, 0, 0, 3)


def test_changed_file_rehashes_and_repoints(conn, root):
    ingest.run_ingest(conn, root)
    target = root / "a.pdf"
    target.write_bytes(b"alpha-changed")
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 10))
    summary = ingest.run_ingest(conn, root)
    assert (summary.changed, summary.new, summary.missing) == (1, 0, 0)
    assert conn.execute("SELECT COUNT(*) n FROM document").fetchone()["n"] == 4
    sha = conn.execute(
        "SELECT d.sha256 s FROM document d JOIN occurrence o USING (document_id) "
        "WHERE o.rel_path = 'a.pdf'"
    ).fetchone()["s"]
    assert sha == hashlib.sha256(b"alpha-changed").hexdigest()


def test_missing_file_marked_not_deleted(conn, root):
    ingest.run_ingest(conn, root)
    (root / "b.pdf").unlink()
    summary = ingest.run_ingest(conn, root)
    assert summary.missing == 1
    assert conn.execute("SELECT COUNT(*) n FROM occurrence").fetchone()["n"] == 3
    row = conn.execute("SELECT missing_since FROM occurrence WHERE rel_path='b.pdf'").fetchone()
    assert row["missing_since"] is not None


def test_missing_sweep_survives_a_frozen_clock(conn, root, monkeypatch):
    """The sweep must not depend on wall-clock resolution (issue #2 verify bounce).

    The Windows system clock ticks every ~0.5-16 ms, so two ingest runs over a small
    tree can share a timestamp. With the clock pinned - the worst case of that - a
    deleted file must still be flagged.
    """
    monkeypatch.setattr(ingest, "_now", lambda: "2026-01-01T00:00:00.000000+00:00")
    ingest.run_ingest(conn, root)
    (root / "b.pdf").unlink()
    summary = ingest.run_ingest(conn, root)
    assert summary.missing == 1
    row = conn.execute("SELECT missing_since FROM occurrence WHERE rel_path='b.pdf'").fetchone()
    assert row["missing_since"] is not None


def test_scan_ids_are_monotonic_and_stamped(conn, root):
    ingest.run_ingest(conn, root)
    ingest.run_ingest(conn, root)
    ids = [r["scan_id"] for r in conn.execute("SELECT scan_id FROM scan ORDER BY scan_id")]
    assert ids == sorted(set(ids)) and len(ids) == 2
    stamped = {r["last_scan_id"] for r in conn.execute("SELECT last_scan_id FROM occurrence")}
    assert stamped == {ids[-1]}


def test_returned_file_clears_missing_since(conn, root):
    ingest.run_ingest(conn, root)
    (root / "b.pdf").unlink()
    ingest.run_ingest(conn, root)
    (root / "b.pdf").write_bytes(b"beta")
    ingest.run_ingest(conn, root)
    row = conn.execute("SELECT missing_since FROM occurrence WHERE rel_path='b.pdf'").fetchone()
    assert row["missing_since"] is None


def test_idempotent_index_state(conn, root):
    ingest.run_ingest(conn, root)
    before = (_rows(conn, "document"), _rows(conn, "occurrence"))
    summary = ingest.run_ingest(conn, root)
    assert (summary.new, summary.changed, summary.missing) == (0, 0, 0)
    assert (_rows(conn, "document"), _rows(conn, "occurrence")) == before


def test_ingest_never_touches_document_bytes(conn, root):
    before = _tree_state(root)
    ingest.run_ingest(conn, root)
    assert _tree_state(root) == before


def test_excluded_and_non_document_files_skipped(conn, root):
    (root / "~$lock.pdf").write_bytes(b"lock")
    (root / "notes.txt").write_text("hi")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.pdf").write_bytes(b"secret")
    summary = ingest.run_ingest(conn, root)
    assert summary.scanned == 3
    paths = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM occurrence")}
    assert paths == {"a.pdf", "b.pdf", "sub/c.png"}


def test_unreadable_file_counted_as_error_not_fatal(conn, root, monkeypatch):
    real = ingest.sha256_file

    def boom(path, *a, **k):
        if path.name == "b.pdf":
            raise PermissionError(path)
        return real(path, *a, **k)

    monkeypatch.setattr(ingest, "sha256_file", boom)
    summary = ingest.run_ingest(conn, root)
    assert summary.errors == 1 and summary.new == 2
    paths = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM occurrence")}
    assert paths == {"a.pdf", "sub/c.png"}
