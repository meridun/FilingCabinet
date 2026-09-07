"""End-to-end smoke: the real CLI, in a real subprocess, over a real tree.

This is the repeatable gating real-run for the phase-2 `ingest` verb (`SMOKE_CMD` in
`sdlc/PROFILE.md`): it invokes `python -m filingcabinet.cli` exactly as a scheduled run would,
rather than calling `cli.main` in-process like `tests/test_cli.py`. It walks the acceptance
criteria in order -- first index, no-op re-run, change, delete, restore -- and asserts the
document tree itself is untouched (`docs/Architecture.md` section 6).
"""

import hashlib
import json
import subprocess
import sys

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<</Type/Catalog>>\nendobj\ntrailer\n"


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


def test_ingest_smoke_end_to_end(tmp_path):
    db = tmp_path / "fc.db"
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "invoice.pdf").write_bytes(PDF_BYTES)
    (root / "sub" / "invoice copy.pdf").write_bytes(PDF_BYTES)  # same bytes, second occurrence
    (root / "scan (1).png").write_bytes(b"PNGDATA-A")
    (root / "Report - conflicted copy 2024.pdf").write_bytes(b"conflicted")
    (root / "notes.txt").write_bytes(b"not a document")

    created = _run(db, "migrate", "--create")
    assert created["created"] is True
    assert _run(db, "status")["migrated"] is True

    fingerprint = _tree_fingerprint(root)

    first = _run(db, "ingest", "--root", str(root))
    assert (first["scanned"], first["new"], first["errors"]) == (4, 4, 0)  # notes.txt skipped

    second = _run(db, "ingest", "--root", str(root))
    assert (second["new"], second["changed"], second["missing"]) == (0, 0, 0)
    assert second["unchanged"] == 4

    (root / "scan (1).png").write_bytes(b"PNGDATA-B-longer")
    (root / "sub" / "invoice copy.pdf").unlink()
    third = _run(db, "ingest", "--root", str(root))
    assert (third["changed"], third["missing"]) == (1, 1)

    (root / "sub" / "invoice copy.pdf").write_bytes(PDF_BYTES)
    fourth = _run(db, "ingest", "--root", str(root))
    assert fourth["missing"] == 0
    assert fourth["unchanged"] == 3  # the returned file is un-missed, not re-reported missing

    after = _tree_fingerprint(root)
    del fingerprint["scan (1).png"], after["scan (1).png"]  # rewritten by the test itself
    del fingerprint["sub/invoice copy.pdf"], after["sub/invoice copy.pdf"]
    assert after == fingerprint  # ingest renamed, moved, or rewrote nothing
