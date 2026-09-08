"""End-to-end smoke: the real `dupes` verbs, in a real subprocess, over a real tree.

The repeatable gating real-run for phase-3 dedup, the sibling of
``tests/test_smoke_ingest.py``: it invokes ``python -m filingcabinet.cli`` exactly as a
scheduled run would rather than calling ``cli.main`` in-process, and walks the acceptance
criteria of issue #3 in order -- migration, exact tier, near tier, subset tier, review
queue, ``dupes label``, threshold precedence, ``--json`` counts -- asserting throughout
that the document tree itself is untouched (``docs/Architecture.md`` section 6).

The near and subset tiers need the optional ``dedup`` extra (PyMuPDF, ImageHash, Pillow)
to render pages; that half is skipped where the extra is absent, exactly as
``tests/test_dedup.py`` guards its render-path tests. The exact tier, the migration, the
threshold precedence, and the tree-untouched invariant need no image stack and always run.
"""

import hashlib
import json
import subprocess
import sys

import pytest

from filingcabinet import dedup

requires_image_stack = pytest.mark.skipif(
    not dedup.phash_available(), reason="optional `dedup` extra (PyMuPDF, ImageHash, Pillow)"
)

# Large filled blocks, so page hashes are far apart on purpose: a text line on an
# otherwise blank page phashes almost identically to any other, which would make the
# controls below meaningless.
PATTERNS = {
    "alpha": [(0, 0, 300, 842)],
    "beta": [(300, 400, 595, 842)],
    "gamma": [(0, 421, 595, 842)],
    "zulu": [(60, 60, 535, 260), (60, 500, 535, 700)],
    "yankee": [(0, 0, 595, 180)],
}


def _run(db, *args, expect_ok=True):
    proc = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "--json", *args],
        capture_output=True,
        text=True,
        check=expect_ok,
    )
    return json.loads(proc.stdout) if expect_ok else proc


def _tree_fingerprint(root):
    return {
        p.relative_to(root).as_posix(): (
            p.stat().st_size,
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _write_pdf(path, pages, title=None):
    import fitz

    doc = fitz.open()
    for name in pages:
        page = doc.new_page(width=595, height=842)
        for rect in PATTERNS[name]:
            page.draw_rect(fitz.Rect(*rect), color=(0, 0, 0), fill=(0, 0, 0))
    if title:  # same rendering, different bytes -> a genuine near-duplicate, not an exact one
        doc.set_metadata({"title": title})
    doc.save(path)
    doc.close()


def test_dupes_smoke_migration_exact_tier_and_thresholds(tmp_path):
    """Migration, exact tier, --json counts, and threshold precedence. No image stack."""
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    payload = b"%PDF-1.4\n1 0 obj\n<</Type/Catalog>>\nendobj\ntrailer\n"
    (root / "invoice.pdf").write_bytes(payload)
    (root / "sub" / "invoice copy.pdf").write_bytes(payload)  # same bytes, second occurrence
    (root / "other.pdf").write_bytes(b"%PDF-1.4\ndifferent bytes\ntrailer\n")

    created = _run(db, "migrate", "--create")
    assert created["created"] is True
    assert "004_dedup.sql" in created["applied"]
    assert _run(db, "migrate")["applied"] == []  # forward-only: re-running is a no-op
    status = _run(db, "status")
    assert (status["migrated"], status["pending"]) == (True, [])

    _run(db, "ingest", "--root", str(root))
    fingerprint = _tree_fingerprint(root)

    report = _run(db, "dupes", "report", "--root", str(root))
    assert (report["exact_groups"], report["exact_documents"]) == (1, 2)
    for key in ("near", "subset", "queued", "hashed_documents", "hashed_pages", "errors"):
        assert isinstance(report[key], int)
    assert isinstance(report["phash_available"], bool)
    assert isinstance(report["truncated"], bool)

    # Threshold precedence: --max-distance > [dedup].phash_max_distance > built-in default.
    config = tmp_path / "config.toml"
    config.write_text("[dedup]\nphash_max_distance = 3\n", encoding="utf-8")
    absent = tmp_path / "no-such-config.toml"

    def reported_distance(config_path, flag=None):
        extra = () if flag is None else ("--max-distance", str(flag))
        args = ("--config", str(config_path), "dupes", "report", "--root", str(root), *extra)
        return _run(db, *args)["max_distance"]

    assert reported_distance(config) == 3  # config beats the built-in default
    assert reported_distance(config, flag=9) == 9  # the flag beats the config
    assert reported_distance(absent) == dedup.DEFAULT_PHASH_MAX_DISTANCE  # documented default

    assert _tree_fingerprint(root) == fingerprint  # dupes moved, renamed, or rewrote nothing


@requires_image_stack
def test_dupes_smoke_near_subset_queue_and_label(tmp_path):
    """Near and subset tiers over really rendered pages, the queue, and `dupes label`."""
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    _write_pdf(root / "a.pdf", ["alpha", "beta"])
    (root / "sub" / "a copy.pdf").write_bytes((root / "a.pdf").read_bytes())  # exact dup
    _write_pdf(root / "near.pdf", ["alpha", "beta"], title="rescanned")  # near dup of a.pdf
    _write_pdf(root / "long.pdf", ["alpha", "beta", "gamma"])  # a.pdf and near.pdf are subsets
    _write_pdf(root / "unrelated.pdf", ["zulu", "yankee"])  # control: must match nothing

    _run(db, "migrate", "--create")
    ingested = _run(db, "ingest", "--root", str(root))
    assert (ingested["scanned"], ingested["new"], ingested["errors"]) == (5, 5, 0)

    fingerprint = _tree_fingerprint(root)

    first = _run(db, "dupes", "report", "--root", str(root))
    assert first["phash_available"] is True
    assert (first["hashed_documents"], first["hashed_pages"]) == (4, 9)
    assert (first["exact_groups"], first["exact_documents"]) == (1, 2)
    assert first["near"] == 1  # a.pdf / near.pdf: identical rendering, different bytes
    assert first["subset"] == 2  # a.pdf and near.pdf both sit inside long.pdf
    assert first["queued"] == 3  # tiers 2 and 3 are queued; the exact tier never is
    assert (first["errors"], first["truncated"]) == (0, False)

    second = _run(db, "dupes", "report", "--root", str(root))
    assert second["queued"] == 0  # idempotent: no duplicate review rows on a re-run
    assert (second["near"], second["subset"]) == (1, 2)
    assert second["hashed_documents"] == 0  # hashes are backfilled once

    pending = _run(db, "dupes", "label", "--list")["pending"]
    assert len(pending) == 3
    assert {row["kind"] for row in pending} == {"near", "subset"}
    assert all(row["document_a"] < row["document_b"] for row in pending if row["kind"] == "near")

    near_row = next(row for row in pending if row["kind"] == "near")
    labelled = _run(
        db,
        "dupes",
        "label",
        "--pair",
        str(near_row["document_a"]),
        str(near_row["document_b"]),
        "--kind",
        "near",
        "--verdict",
        "dup",
    )
    assert labelled["verdict"] == "dup"

    assert len(_run(db, "dupes", "label", "--list")["pending"]) == 2  # the judged pair is resolved
    exported = _run(db, "dupes", "label", "--export")["labels"]
    assert len(exported) == 1
    assert (exported[0]["kind"], exported[0]["verdict"]) == ("near", "dup")

    third = _run(db, "dupes", "report", "--root", str(root))
    assert third["queued"] == 0
    assert len(_run(db, "dupes", "label", "--list")["pending"]) == 2  # a verdict is never reopened

    # Nothing under the document root moved, was renamed, or was rewritten: dedup only
    # reports and queues (docs/Architecture.md section 6).
    assert _tree_fingerprint(root) == fingerprint
