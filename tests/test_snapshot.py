from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from filingcabinet import db, snapshot

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _migrated_db(path: Path) -> Path:
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    return path


def _insert_document(path: Path, sha: str) -> None:
    stamp = NOW.isoformat(timespec="seconds")
    conn = db.connect(path)
    with conn:
        conn.execute(
            "INSERT INTO document (sha256, size_bytes, first_seen_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (sha, len(sha), stamp, stamp),
        )
    conn.close()


def _touch_snapshots(snapshot_dir: Path, stamps: list[datetime]) -> list[Path]:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for stamp in stamps:
        p = snapshot_dir / snapshot.snapshot_name(stamp)
        p.write_bytes(b"placeholder")
        made.append(p)
    return made


def test_create_snapshot_makes_consistent_copy(tmp_path):
    dbp = _migrated_db(tmp_path / "data" / "fc.db")
    _insert_document(dbp, "a" * 64)
    before = dbp.read_bytes()

    target = snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)

    assert target.name == "filingcabinet-20260601T120000Z.db"
    assert target.parent == tmp_path / "snaps"
    conn = db.connect(target)
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1
    conn.close()
    assert dbp.read_bytes() == before  # the live index is untouched


def test_create_snapshot_requires_migrated(tmp_path):
    dbp = tmp_path / "fc.db"
    conn = db.connect(dbp)  # creates the file without applying migrations
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(db.NotMigratedError):
        snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)


def test_create_snapshot_refuses_missing_database(tmp_path):
    with pytest.raises(snapshot.SnapshotError):
        snapshot.create_snapshot(tmp_path / "nope.db", tmp_path / "snaps", now=NOW)


def test_create_snapshot_refuses_existing_target(tmp_path):
    dbp = _migrated_db(tmp_path / "fc.db")
    snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)


def test_rotate_keeps_daily_window_and_weeklies(tmp_path):
    snaps = tmp_path / "snaps"
    stamps = [NOW - timedelta(days=n) for n in range(0, 180, 3)]
    _touch_snapshots(snaps, stamps)

    deleted = snapshot.rotate(snaps, now=NOW)
    kept = {p.name for p in snapshot.list_snapshots(snaps)}

    assert kept.isdisjoint({p.name for p in deleted})
    assert snapshot.snapshot_name(stamps[0]) in kept  # newest always survives
    for stamp in stamps:
        name = snapshot.snapshot_name(stamp)
        age = NOW - stamp
        if age < timedelta(days=snapshot.DAILY_RETENTION_DAYS):
            assert name in kept, f"daily-window snapshot {name} was rotated away"
        elif age > timedelta(weeks=snapshot.WEEKLY_RETENTION_WEEKS):
            assert name not in kept or name == snapshot.snapshot_name(stamps[0])
    # inside the weekly window, exactly one snapshot survives per ISO week
    weekly = [
        p
        for p in snapshot.list_snapshots(snaps)
        if NOW - snapshot.parse_snapshot_time(p) >= timedelta(days=snapshot.DAILY_RETENTION_DAYS)
    ]
    weeks = [snapshot.parse_snapshot_time(p).isocalendar()[:2] for p in weekly]
    assert len(weeks) == len(set(weeks))
    assert deleted, "a 6-month spread must rotate something"


def test_rotate_keeps_newest_even_when_ancient(tmp_path):
    snaps = tmp_path / "snaps"
    made = _touch_snapshots(snaps, [NOW - timedelta(days=900)])
    assert snapshot.rotate(snaps, now=NOW) == []
    assert made[0].exists()


def test_rotate_ignores_foreign_files(tmp_path):
    snaps = tmp_path / "snaps"
    _touch_snapshots(snaps, [NOW - timedelta(days=n) for n in (0, 400, 500)])
    notes = snaps / "notes.txt"
    other = snaps / "something.db"
    desync = snaps / "filingcabinet-notatimestamp.db"
    for stray in (notes, other, desync):
        stray.write_bytes(b"keep me")

    snapshot.rotate(snaps, now=NOW)

    assert notes.exists() and other.exists() and desync.exists()


def test_parse_snapshot_time_rejects_foreign_names(tmp_path):
    assert snapshot.parse_snapshot_time("notes.txt") is None
    assert snapshot.parse_snapshot_time("filingcabinet-nope.db") is None
    assert snapshot.parse_snapshot_time("filingcabinet-20260601T120000Z.db") == NOW


def test_validate_rejects_corrupt_and_unmigrated(tmp_path):
    garbage = tmp_path / "filingcabinet-20260601T120000Z.db"
    garbage.write_bytes(b"not a database at all, not even close" * 10)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.validate_snapshot(garbage)

    bare = tmp_path / "bare.db"
    conn = db.connect(bare)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(snapshot.SnapshotError):
        snapshot.validate_snapshot(bare)

    with pytest.raises(snapshot.SnapshotError):
        snapshot.validate_snapshot(tmp_path / "absent.db")


def test_validate_accepts_a_real_snapshot(tmp_path):
    dbp = _migrated_db(tmp_path / "fc.db")
    target = snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)
    report = snapshot.validate_snapshot(target)
    assert report["integrity"] == "ok" and "001_init.sql" in report["applied"]


def test_restore_banks_rescue_copy_and_clears_sidecars(tmp_path):
    dbp = _migrated_db(tmp_path / "data" / "fc.db")
    _insert_document(dbp, "a" * 64)
    target = snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)
    _insert_document(dbp, "b" * 64)
    live_before = dbp.read_bytes()
    for sidecar in snapshot.sidecar_paths(dbp):
        sidecar.write_bytes(b"stale")

    result = snapshot.restore_snapshot(dbp, target, now=NOW)

    assert sorted(result["cleared_sidecars"]) == ["fc.db-shm", "fc.db-wal"]
    for sidecar in snapshot.sidecar_paths(dbp):
        assert not sidecar.exists() or sidecar.read_bytes() != b"stale"
    rescue = Path(result["rescue_copy"])
    assert rescue.exists() and rescue.read_bytes() == live_before
    assert rescue.parent == dbp.parent / snapshot.RESCUE_DIRNAME
    conn = db.connect(dbp)
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 1
    conn.close()


def test_restore_without_live_db_records_no_rescue_copy(tmp_path):
    dbp = _migrated_db(tmp_path / "fc.db")
    target = snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)
    dbp.unlink()
    result = snapshot.restore_snapshot(dbp, target, now=NOW)
    assert result["rescue_copy"] is None and dbp.is_file()


def test_restore_migrates_forward(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    real = db.DEFAULT_MIGRATIONS_DIR / "001_init.sql"
    (migrations / "001_init.sql").write_text(real.read_text(encoding="utf-8"), encoding="utf-8")

    dbp = tmp_path / "fc.db"
    conn = db.connect(dbp)
    db.migrate(conn, migrations)
    conn.close()
    target = snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)

    later = migrations / "099_later.sql"
    later.write_text("CREATE TABLE later (x INTEGER);\n", encoding="utf-8")
    result = snapshot.restore_snapshot(dbp, target, now=NOW, migrations_dir=migrations)

    assert result["applied"] == ["099_later.sql"]
    assert "later" in result["row_counts"]


def test_restore_reports_row_counts(tmp_path):
    dbp = _migrated_db(tmp_path / "fc.db")
    for n in range(3):
        _insert_document(dbp, str(n) * 64)
    target = snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)

    result = snapshot.restore_snapshot(dbp, target, now=NOW)

    assert result["row_counts"]["document"] == 3
    assert result["row_counts"]["schema_migrations"] >= 1
    assert all(isinstance(v, int) for v in result["row_counts"].values())


def test_restore_aborts_before_touching_live_db_on_invalid_snapshot(tmp_path):
    dbp = _migrated_db(tmp_path / "fc.db")
    _insert_document(dbp, "a" * 64)
    before = dbp.read_bytes()
    bad = tmp_path / "snaps" / "filingcabinet-20260601T120000Z.db"
    bad.parent.mkdir()
    bad.write_bytes(b"corrupt" * 100)

    with pytest.raises(snapshot.SnapshotError):
        snapshot.restore_snapshot(dbp, bad, now=NOW)

    assert dbp.read_bytes() == before
    assert not (dbp.parent / snapshot.RESCUE_DIRNAME).exists()


def test_snapshot_and_restore_touch_no_documents(tmp_path):
    """Metadata-only invariant (docs/Architecture.md §6): the document root is never written."""
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "a.pdf"
    doc.write_bytes(b"alpha")
    stat_before = (doc.read_bytes(), doc.stat().st_mtime_ns)

    dbp = _migrated_db(tmp_path / "data" / "fc.db")
    target = snapshot.create_snapshot(dbp, tmp_path / "snaps", now=NOW)
    snapshot.restore_snapshot(dbp, target, now=NOW)

    assert list(root.iterdir()) == [doc]
    assert (doc.read_bytes(), doc.stat().st_mtime_ns) == stat_before
