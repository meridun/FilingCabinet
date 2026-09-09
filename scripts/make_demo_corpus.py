#!/usr/bin/env python
"""Build a synthetic, fully fabricated FilingCabinet index for the phase-8 spike (issue #8).

The graphify experiment (docs/Development_GraphifyExperiment.md) needs a corpus that can be
exported, graphed, and searched without touching anyone's real documents. This generator
writes a migrated index whose fabricated documents deliberately share entities across
documents — three vendors, two people, two account numbers — plus single-document
distractors, so "did the graph find a cross-document link?" has a knowable answer and a
real negative control.

Every name, date, and account number here is invented. The script only ever writes a
database it created itself under a caller-named ``--db``; it never reads or moves a user
document (docs/Architecture.md §6), and the same in-repo refusal as the exporter keeps the
generated index out of this repo (docs/Architecture.md §8).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from filingcabinet import db

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_ocr_text import refuse_inside_repo  # noqa: E402  (sibling script, not a package)

VENDORS = ("Northwind Utilities", "Harborline Insurance", "Cedarpoint Clinic")
PEOPLE = ("Alex Marlowe", "Priya Ramanathan")
ACCOUNTS = ("ACCT-40771925", "ACCT-88213604")

# (party slug, doc_type, doc_date, page 1, page 2). Linked documents first: each shares at
# least one entity with another document. The distractors that follow share nothing.
LINKED: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "northwind-utilities",
        "invoice",
        "2025-01-14",
        "Northwind Utilities electricity invoice for Alex Marlowe. "
        "Account ACCT-40771925. Billing period December 2024.",
        "Amount due 84.20 payable to Northwind Utilities. "
        "Questions about ACCT-40771925 quote invoice NU-1141.",
    ),
    (
        "northwind-utilities",
        "statement",
        "2025-04-02",
        "Northwind Utilities annual statement for account ACCT-40771925.",
        "Total consumed 3140 kWh. Northwind Utilities tariff Standard Home.",
    ),
    (
        "northwind-utilities",
        "letter",
        "2025-05-19",
        "Northwind Utilities service letter addressed to Priya Ramanathan "
        "about a meter exchange appointment.",
        "The Northwind Utilities engineer will call at the property on 2 June 2025.",
    ),
    (
        "harborline-insurance",
        "policy",
        "2025-02-08",
        "Harborline Insurance home policy schedule. Policyholder Alex Marlowe. "
        "Account ACCT-88213604.",
        "Harborline Insurance cover runs to 8 February 2026. Excess 250.",
    ),
    (
        "harborline-insurance",
        "notice",
        "2026-01-11",
        "Harborline Insurance renewal notice for account ACCT-88213604.",
        "Renewal premium 412.60. Contact Harborline Insurance to amend cover.",
    ),
    (
        "harborline-insurance",
        "letter",
        "2025-09-30",
        "Harborline Insurance claim settlement letter covering treatment invoiced "
        "by Cedarpoint Clinic.",
        "Harborline Insurance has paid Cedarpoint Clinic directly. No excess applies.",
    ),
    (
        "cedarpoint-clinic",
        "report",
        "2025-09-12",
        "Cedarpoint Clinic appointment summary for Priya Ramanathan.",
        "Cedarpoint Clinic follow-up booked for October. No prescription issued.",
    ),
    (
        "cedarpoint-clinic",
        "letter",
        "2025-08-21",
        "Cedarpoint Clinic referral letter concerning Alex Marlowe.",
        "Cedarpoint Clinic asks the practice to confirm the referral by post.",
    ),
    (
        "meridian-bank",
        "receipt",
        "2025-01-20",
        "Payment confirmation from Alex Marlowe to account ACCT-40771925.",
        "Transfer reference MB-88120. Funds cleared same day.",
    ),
    (
        "meridian-bank",
        "form",
        "2025-02-15",
        "Direct debit mandate signed by Priya Ramanathan for account ACCT-88213604.",
        "The mandate authorises monthly collection from the named current account.",
    ),
    (
        "northwind-utilities",
        "letter",
        "2025-11-03",
        "Northwind Utilities letter confirming that Harborline Insurance has been "
        "notified of the supply interruption.",
        "Northwind Utilities apologises for the outage of 28 October 2025.",
    ),
)

# One unique term each, appearing in exactly one document: the negative controls.
DISTRACTORS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "zephyr-bicycle-repair",
        "receipt",
        "2025-03-04",
        "Zephyr Bicycle Repair service receipt.",
        "Zephyr Bicycle Repair replaced two brake cables. Paid in cash.",
    ),
    (
        "tidewell-garden-centre",
        "invoice",
        "2025-06-17",
        "Tidewell Garden Centre invoice for topsoil delivery.",
        "Tidewell Garden Centre delivery scheduled for the following Tuesday.",
    ),
    (
        "quillon-bookbinding",
        "quote",
        "2025-07-25",
        "Quillon Bookbinding quotation for rebinding two volumes.",
        "Quillon Bookbinding estimates four weeks from receipt.",
    ),
    (
        "marrowfield-removals",
        "invoice",
        "2025-10-09",
        "Marrowfield Removals invoice for a half-day van hire.",
        "Marrowfield Removals crew of two, mileage included.",
    ),
)

SPECS = LINKED + DISTRACTORS


def specs(documents: int) -> list[tuple[str, str, str, str, str]]:
    """The document specs for a corpus of ``documents`` rows, padded with distractors."""
    if documents < 1:
        raise ValueError("documents must be at least 1")
    chosen = list(SPECS[:documents])
    index = len(SPECS)
    while len(chosen) < documents:
        index += 1
        token = f"Fillerton Supplies (batch {index:03d})"
        chosen.append(
            (
                f"fillerton-{index:03d}",
                "letter",
                "2025-12-01",
                f"{token} routine correspondence.",
                f"{token} requires no reply.",
            )
        )
    return chosen


def _insert(conn, spec: tuple[str, str, str, str, str], ordinal: int, now: str) -> int:
    party, doc_type, doc_date, page_one, page_two = spec
    text = f"{page_one}\n\n{page_two}"
    sha = hashlib.sha256(f"{ordinal}:{text}".encode("utf-8")).hexdigest()
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, mime, page_count, doc_date, party, doc_type, "
        "ocr_text, ocr_source, first_seen_at, updated_at) "
        "VALUES (?, ?, 'application/pdf', 2, ?, ?, ?, ?, 'local_text', ?, ?)",
        (sha, len(text), doc_date, party, doc_type, text, now, now),
    )
    document_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at) "
        "VALUES (?, ?, 0.0, ?, ?)",
        (document_id, f"inbox/{ordinal:05d}-{party}-{doc_type}.pdf", len(text), now),
    )
    for page_number, page_text in enumerate((page_one, page_two), start=1):
        conn.execute(
            "INSERT INTO page_ocr (document_id, page_number, text, confidence, rung, ocr_source, "
            "status, updated_at) VALUES (?, ?, ?, 0.99, 'local_text', 'local_text', 'ok', ?)",
            (document_id, page_number, page_text, now),
        )
    return document_id


def build(db_path: Path, *, documents: int = 15) -> dict:
    """Create and populate a migrated demo index at ``db_path``; returns a summary dict."""
    resolved = refuse_inside_repo(db_path)
    conn = db.connect(resolved)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        applied = db.migrate(conn)
        chosen = specs(documents)
        with conn:
            document_ids = [
                _insert(conn, spec, ordinal, now) for ordinal, spec in enumerate(chosen, start=1)
            ]
    finally:
        conn.close()
    return {
        "db": str(resolved),
        "applied": applied,
        "document_count": len(document_ids),
        "document_ids": document_ids,
        "vendors": list(VENDORS),
        "people": list(PEOPLE),
        "accounts": list(ACCOUNTS),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_demo_corpus",
        description="Build a synthetic FilingCabinet index for the phase-8 graphify spike.",
    )
    parser.add_argument("--db", required=True, help="database path to create (outside this repo)")
    parser.add_argument("--documents", type=int, default=15, help="how many documents to fabricate")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = build(Path(args.db), documents=args.documents)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"built {summary['document_count']} fabricated documents at {summary['db']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
