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
import pathlib
import subprocess
import sys

import pytest

from filingcabinet import ocr

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
    assert written["plan_version"] == 1 and len(written["entries"]) == 2

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


# --- issue #19: per-rule doc_date selection ------------------------------------------------
#
# The gating real run for `date_regex` / `date = "first" | "last"`: the same subprocess CLI
# walk as above, over a statement whose front page carries three dates (an issue date, a
# period start, a period end) the way a real statement does. Unit tests cover the selectors in
# isolation; this proves the selected date reaches the *proposed name* and that the plan file
# records where it came from.

STATEMENT_LINES = (
    "Northwind Supplies monthly statement of account.",
    "Issue date 02/01/2026.",
    "Statement period 05/01/2026 to 04/02/2026.",
    "Balance carried forward from the previous statement of account.",
    "Printed 09/03/2026 for the account holder's records.",
)

STATEMENT_TAXONOMY = """
version = 1
doc_types = ["statement"]
date_order = "dmy"

[parties.northwind]
display = "Northwind Supplies"
aliases = ["northwind supplies"]

[[rules]]
id = "northwind-statement"
party = "northwind"
doc_type = "statement"
all = ["northwind"]
any = ["statement of account"]
folder = "Suppliers/Northwind"
tags = ["supplier"]
priority = 90
"""

# The shipped scaffold's worked example, verbatim: a TOML literal string whose capture is a
# short window after "to", left for the date parser to resolve.
PERIOD_END_REGEX = r"date_regex = 'statement period.{0,60}?\bto\b\s*([^\r\n]{0,30})'" + "\n"
MISSING_REGEX = r"date_regex = 'no such phrase here (\d{4})'" + "\n"


@requires_pymupdf
def test_propose_smoke_per_rule_doc_date_selection(tmp_path):
    """Each date selector, end to end: the chosen date lands in the proposed name."""
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    (root / "inbox").mkdir(parents=True)
    _write_pdf(root / "inbox" / "scan-101.pdf", list(STATEMENT_LINES))
    taxonomy = tmp_path / "taxonomy.toml"
    plan_dir = tmp_path / "plans"  # outside the root, as always

    assert _run(db, "migrate", "--create")["created"] is True
    assert _run(db, "ingest", "--root", str(root))["new"] == 1
    assert _run(db, "ocr", "run", "--root", str(root))["errors"] == 0
    fingerprint = _tree_fingerprint(root)

    def propose(rule_keys):
        taxonomy.write_text(STATEMENT_TAXONOMY + rule_keys, encoding="utf-8")
        out = _run(db, "propose", "--root", str(root), "--taxonomy", str(taxonomy),
                   "--plan-dir", str(plan_dir))
        entry = out["entries"][0]
        written = json.loads(pathlib.Path(out["plan"]).read_text(encoding="utf-8"))
        # The plan file carries what the JSON output claims - the plan is what phase 6 reads.
        assert written["entries"][0]["date_source"] == entry["date_source"]
        assert written["plan_version"] == 1
        return entry

    def name(entry):
        return entry["target_path"].rsplit("/", 1)[-1]

    # Neither key: unchanged first-date behaviour - the issue date, the first date in the text.
    plain = propose("")
    assert name(plain) == "2026-01-02_Northwind_Supplies_statement.pdf"
    assert plain["date_source"] == "first"

    # date_regex names the period end, beating both the earlier issue date and the last date.
    regexed = propose(PERIOD_END_REGEX)
    assert name(regexed) == "2026-02-04_Northwind_Supplies_statement.pdf"
    assert regexed["date_source"] == "rule-regex"

    # The selector on its own: "last" is the printed-on date at the foot, "first" is today's.
    last = propose('date = "last"\n')
    assert name(last) == "2026-03-09_Northwind_Supplies_statement.pdf"
    assert last["date_source"] == "last"
    assert name(propose('date = "first"\n')) == name(plain)

    # A regex that finds nothing degrades to the selector rather than erroring or blanking.
    fallen_back = propose(MISSING_REGEX + 'date = "last"\n')
    assert name(fallen_back) == name(last)
    assert fallen_back["date_source"] == "last"

    # The agent verdict still outranks every rule-side selection.
    _run(db, "classify", "--document", str(plain["document_id"]),
         "--party", "Northwind Supplies", "--doc-type", "statement",
         "--doc-date", "2026-06-30", "--folder", "Suppliers/Northwind")
    agent = propose(PERIOD_END_REGEX)
    assert name(agent) == "2026-06-30_Northwind_Supplies_statement.pdf"
    assert agent["date_source"] == "agent"

    # Choosing a different date is still a *proposal*: nothing under the root moved or changed.
    assert _tree_fingerprint(root) == fingerprint
    assert not (root / "plans").exists()


@pytest.mark.parametrize(
    "date_keys, fragment",
    [
        ('date = "middle"\n', "rules.northwind-statement.date must be one of first, last"),
        (r"date_regex = '(20\d\d'" + "\n", "rules.northwind-statement.date_regex:"),
        (r"date_regex = 'period to \d+'" + "\n", "must have exactly one capture group"),
        (r"date_regex = '(period) to (\d+)'" + "\n", "must have exactly one capture group"),
    ],
)
def test_propose_smoke_rejects_bad_date_keys_at_load(tmp_path, date_keys, fragment):
    """Bad date keys fail at load, before any document is touched - no toolchain needed."""
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    root.mkdir()
    taxonomy = tmp_path / "taxonomy.toml"
    taxonomy.write_text(STATEMENT_TAXONOMY + date_keys, encoding="utf-8")
    assert _run(db, "migrate", "--create")["created"] is True

    proc = subprocess.run(
        [sys.executable, "-m", "filingcabinet.cli", "--db", str(db), "propose",
         "--root", str(root), "--taxonomy", str(taxonomy),
         "--plan-dir", str(tmp_path / "plans")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert fragment in proc.stderr
    assert not (tmp_path / "plans").exists()
