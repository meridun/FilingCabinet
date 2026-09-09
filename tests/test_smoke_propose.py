"""End-to-end smoke: the real `propose` and `classify` verbs over a real tree.

The repeatable gating real-run for phase-5, the sibling of ``tests/test_smoke_ocr.py``: it
invokes ``python -m filingcabinet.cli`` by subprocess exactly as a scheduled run would rather
than calling ``cli.main`` in-process, and walks issue #5's acceptance criteria in order --
migrate, ingest, `ocr run`, `propose --json` (rule match vs. unclassified), `classify`, and the
re-`propose` that shows the agent verdict outranking -- asserting throughout that the document
tree is byte-identical before and after and that the plan file lands *outside* the root
(``docs/Architecture.md`` section 6: tools never move or rename documents unasked).

Authoring the PDF fixtures needs PyMuPDF (the optional ``ocr`` extra); that part is skipped
where it is absent. The migration and the plan-directory guard always run.
"""

import hashlib
import json
import subprocess
import sys

import pytest

from filingcabinet import ocr, organize

requires_pymupdf = pytest.mark.skipif(
    ocr.pymupdf_version() is None, reason="optional `ocr` extra (PyMuPDF)"
)

INVOICE = (
    "Northwind Supplies tax invoice no 2026-014 for office consumables delivered to the "
    "Camden depot on 3 February 2026. Payment is due within thirty days by bank transfer "
    "to the account named on this page. Please quote the invoice number on every remittance."
)
LETTER = (
    "Dear neighbour, the hedge along the boundary will be trimmed next Tuesday and the "
    "clippings taken away the same afternoon. No action is needed from you. Kind regards "
    "from the household at number eleven, written by hand on plain unheaded paper."
)

TAXONOMY = """
version = 1
doc_types = ["invoice", "statement"]
date_order = "dmy"

[parties.northwind]
display = "Northwind Supplies"
aliases = ["northwind supplies"]

[[rules]]
id = "northwind-invoice"
party = "northwind"
doc_type = "invoice"
all = ["northwind"]
any = ["invoice", "tax invoice"]
folder = "Suppliers/Northwind"
tags = ["supplier"]
priority = 100
"""


def _run(db, *args):
    proc = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "--json", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


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


def _write_pdf(path, lines):
    import fitz

    doc = fitz.open()
    for line in lines:
        page = doc.new_page()
        page.insert_text((72, 100), line, fontsize=9)
    doc.save(path)
    doc.close()


def _wrap(text):
    return [text[i : i + 90] for i in range(0, len(text), 90)]


def test_propose_smoke_refuses_a_plan_dir_inside_the_root(tmp_path):
    """The invariant guard runs with no toolchain at all: plans are never written to the tree."""
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    root.mkdir()
    assert _run(db, "migrate", "--create")["created"] is True
    proc = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "propose",
         "--root", str(root), "--plan-dir", str(root / "plans")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "inside the document root" in proc.stderr
    assert not (root / "plans").exists()


@requires_pymupdf
def test_propose_smoke_end_to_end(tmp_path):
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    root.mkdir()
    (root / "inbox").mkdir()
    _write_pdf(root / "inbox" / "scan-001.pdf", _wrap(INVOICE))
    _write_pdf(root / "inbox" / "scan-002.pdf", _wrap(LETTER))
    taxonomy = tmp_path / "taxonomy.toml"
    taxonomy.write_text(TAXONOMY, encoding="utf-8")
    plan_dir = tmp_path / "plans"  # deliberately outside root

    assert _run(db, "migrate", "--create")["created"] is True
    ingested = _run(db, "ingest", "--root", str(root))
    assert (ingested["scanned"], ingested["new"]) == (2, 2)
    assert _run(db, "ocr", "run", "--root", str(root))["errors"] == 0

    fingerprint = _tree_fingerprint(root)

    first = _run(db, "propose", "--root", str(root), "--taxonomy", str(taxonomy),
                 "--plan-dir", str(plan_dir))
    by_path = {entry["current_path"]: entry for entry in first["entries"]}
    invoice = by_path["inbox/scan-001.pdf"]
    letter = by_path["inbox/scan-002.pdf"]

    assert invoice["provenance"] == "rule" and invoice["rule_id"] == "northwind-invoice"
    assert invoice["status"] == "move"
    assert invoice["target_path"] == (
        "Suppliers/Northwind/2026-02-03_Northwind_Supplies_invoice.pdf"
    )
    assert invoice["tags"] == ["supplier"]
    assert letter["status"] == "unclassified" and letter["provenance"] is None
    assert (first["rule_matched"], first["unclassified"]) == (1, 1)

    # The plan is a file, outside the root, in the versioned shape phase 6 will read back.
    plan_path = tmp_path / "plans" / f"{first['plan_id']}.json"
    assert str(plan_path) == first["plan"]
    assert plan_dir.exists() and not (root / "plans").exists()
    written = json.loads(plan_path.read_text(encoding="utf-8"))
    assert written["plan_version"] == organize.PLAN_VERSION
    assert len(written["entries"]) == 2

    # No file under the root was created, renamed, or rewritten.
    assert _tree_fingerprint(root) == fingerprint

    classified = _run(db, "classify", "--document", str(letter["document_id"]),
                      "--party", "Household", "--doc-type", "letter",
                      "--detail", "hedge", "--doc-date", "2026-02-10",
                      "--folder", "Correspondence", "--note", "handwritten, no letterhead")
    assert classified["provenance"] == "agent"
    assert classified["suggested_rule"].startswith("[[rules]]")

    second = _run(db, "propose", "--root", str(root), "--taxonomy", str(taxonomy),
                  "--plan-dir", str(plan_dir))
    promoted = {e["current_path"]: e for e in second["entries"]}["inbox/scan-002.pdf"]
    assert promoted["provenance"] == "agent" and promoted["status"] == "move"
    assert promoted["target_path"] == "Correspondence/2026-02-10_Household_letter_hedge.pdf"
    assert (second["rule_matched"], second["agent_matched"]) == (1, 1)
    assert second["unclassified"] == 0

    dry = _run(db, "propose", "--root", str(root), "--taxonomy", str(taxonomy),
               "--plan-dir", str(plan_dir), "--dry-run")
    assert dry["plan"] is None and dry["dry_run"] is True
    assert len(list(plan_dir.glob("*.json"))) == 2  # the dry run added nothing

    assert _tree_fingerprint(root) == fingerprint  # still byte-identical after every step
