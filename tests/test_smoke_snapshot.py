"""End-to-end smoke: `snapshot` and `restore` through the real CLI, in a real subprocess.

This is the repeatable gating real-run for the index snapshot/restore verbs (`SMOKE_CMD` in
`sdlc/PROFILE.md`): it invokes `python -m filingcabinet.cli` exactly as a human or a scheduled
run would, rather than calling `cli.main` in-process like `tests/test_cli.py`. It walks the
acceptance criteria in order -- WAL-consistent snapshot, rotation, refusal to restore an invalid
snapshot, rescue copy, sidecar clearing, forward migration, row counts -- and asserts the
document tree itself is untouched (`docs/Architecture.md` sections 6 and 8).
"""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<</Type/Catalog>>\nendobj\ntrailer\n"
TS_FORMAT = "%Y%m%dT%H%M%SZ"


def _run(db, *args, snapshot_dir=None, expect=0):
    """Invoke the CLI in a subprocess. Returns parsed JSON on success, stderr on failure."""
    cmd = [sys.executable, "-m", "filingcabinet.cli", "--db", str(db)]
    if snapshot_dir is not None:
        cmd += ["--snapshot-dir", str(snapshot_dir)]
    cmd += ["--json", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == expect, (
        f"{args}: exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    return json.loads(proc.stdout) if expect == 0 else proc.stderr


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


def _wait_for_next_second():
    """Rescue-copy filenames carry a one-second stamp; keep two restores from colliding."""
    start = int(time.time())
    while int(time.time()) == start:
        time.sleep(0.05)


def _query(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_snapshot_restore_smoke_end_to_end(tmp_path):
    db = tmp_path / "data" / "filingcabinet.db"
    db.parent.mkdir()
    snaps = tmp_path / "snapshots"
    root = tmp_path / "root"
    root.mkdir()
    (root / "invoice.pdf").write_bytes(PDF_BYTES)
    (root / "scan.png").write_bytes(b"PNGDATA")

    assert _run(db, "migrate", "--create")["created"] is True
    assert _run(db, "ingest", "--root", str(root))["new"] == 2
    fingerprint = _tree_fingerprint(root)

    # AC1 -- the snapshot is a VACUUM INTO copy, not a raw file copy: a row committed to
    # the WAL but not yet checkpointed into the main .db file must still be in it.
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute(
        "INSERT INTO document (sha256, size_bytes, first_seen_at, updated_at) "
        "VALUES ('wal-only', 1, '2026-01-01', '2026-01-01')"
    )
    live.commit()
    wal = Path(str(db) + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0  # the row lives in the WAL, not the .db
    first = _run(db, "snapshot", snapshot_dir=snaps)
    live.close()

    created = Path(first["snapshot"])
    assert created.parent == snaps and created.name.startswith("filingcabinet-")
    assert datetime.strptime(created.name[len("filingcabinet-") : -len(".db")], TS_FORMAT)
    assert not Path(str(created) + "-wal").exists()  # a snapshot carries no sidecars
    assert _query(created, "SELECT COUNT(*) FROM document WHERE sha256 = 'wal-only'")[0][0] == 1

    # AC2 -- repeated runs rotate instead of growing without bound. Age the first snapshot
    # out (which also frees its same-second filename) and fabricate a history around it.
    now = datetime.now(timezone.utc)

    def aged(days):
        stamp = (now - timedelta(days=days)).strftime(TS_FORMAT)
        target = snaps / f"filingcabinet-{stamp}.db"
        shutil.copy2(created, target)
        return target

    inside_daily = [aged(1), aged(3), aged(13)]  # < 14-day daily window
    weeklies = [aged(20), aged(27), aged(40)]  # distinct ISO weeks inside the 12-week window
    ancient = aged(200)  # beyond the weekly window
    foreign = snaps / "notes.txt"
    foreign.write_bytes(b"not a snapshot")
    created.unlink()

    second = _run(db, "snapshot", snapshot_dir=snaps)
    assert [Path(p).name for p in second["rotated"]] == [ancient.name]
    kept = {p.name for p in snaps.glob("filingcabinet-*.db")}
    assert kept == {p.name for p in inside_daily + weeklies} | {Path(second["snapshot"]).name}
    assert foreign.is_file()  # foreign files in a synced folder are never rotated away

    # AC3 -- `restore latest` refuses an unusable snapshot and says why, without touching
    # the live index. The corrupt file is made the newest so `latest` selects it.
    newest_stamp = datetime.strptime(
        Path(second["snapshot"]).name[len("filingcabinet-") : -len(".db")], TS_FORMAT
    )
    trap_stamp = (newest_stamp + timedelta(seconds=1)).strftime(TS_FORMAT)
    booby_trap = snaps / f"filingcabinet-{trap_stamp}.db"
    booby_trap.write_bytes(b"this is not a database")
    before_bytes = db.read_bytes()
    stderr = _run(db, "restore", "latest", snapshot_dir=snaps, expect=2)
    assert booby_trap.name in stderr and "not a readable database" in stderr
    assert db.read_bytes() == before_bytes
    booby_trap.unlink()

    # A structurally valid SQLite file that is not an index is refused just as clearly.
    not_an_index = tmp_path / "empty.db"
    conn = sqlite3.connect(not_an_index)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    stderr = _run(db, "restore", str(not_an_index), expect=2)
    assert "not a migrated index" in stderr and "schema_migrations" in stderr
    assert db.read_bytes() == before_bytes

    # AC6 -- an older-schema snapshot lands on current schema: roll the newest snapshot
    # back to the pre-003 shape so restore has a real forward migration to run.
    newest = Path(second["snapshot"])
    conn = sqlite3.connect(newest)
    conn.execute("DROP TABLE scan")
    conn.execute("ALTER TABLE occurrence DROP COLUMN last_scan_id")
    conn.execute("DELETE FROM schema_migrations WHERE version = '003_scan.sql'")
    conn.commit()
    conn.close()

    # Diverge the live index from the snapshot, and leave stale sidecars behind as a
    # crashed run would.
    (root / "late.pdf").write_bytes(b"%PDF-1.4 late")
    assert _run(db, "ingest", "--root", str(root))["new"] == 1
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).write_bytes(b"stale sidecar")
    pre_restore_bytes = db.read_bytes()

    result = _run(db, "restore", "latest", snapshot_dir=snaps)

    assert result["restored_from"] == str(newest)
    assert result["applied"] == ["003_scan.sql"]  # AC6: migrated forward on the way in
    rescue = Path(result["rescue_copy"])  # AC4: the replaced index is banked first
    assert rescue.is_file() and rescue.parent == db.parent / "rescue"
    assert rescue.read_bytes() == pre_restore_bytes
    assert sorted(result["cleared_sidecars"]) == [  # AC5
        f"{db.name}-shm",
        f"{db.name}-wal",
    ]
    for suffix in ("-wal", "-shm"):
        assert not Path(str(db) + suffix).exists()
    counts = result["row_counts"]  # AC7: per-table row counts
    assert counts["document"] == 3 and counts["occurrence"] == 2 and counts["scan"] == 0
    assert _run(db, "status")["migrated"] is True
    paths = {row[0] for row in _query(db, "SELECT rel_path FROM occurrence")}
    assert paths == {"invoice.pdf", "scan.png"}  # the post-snapshot ingest was rolled back

    # AC4, second half -- the rescue copy is a real undo: restoring it brings the replaced
    # index back. Rescue filenames have one-second resolution, so hold the two restores
    # apart; the same-second collision that breaks this undo is pinned deterministically
    # in tests/test_snapshot.py, which can inject the clock.
    _wait_for_next_second()
    undo = _run(db, "restore", str(rescue))
    assert undo["applied"] == []
    paths = {row[0] for row in _query(db, "SELECT rel_path FROM occurrence")}
    assert "late.pdf" in paths, "restoring the rescue copy did not undo the restore"

    # AC8 -- no document byte was read, moved, or modified by either verb.
    after = _tree_fingerprint(root)
    del after["late.pdf"]  # written by the test itself, after the fingerprint was taken
    assert after == fingerprint
