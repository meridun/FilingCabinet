"""Index snapshot and restore (docs/Architecture.md §1).

The live index runs in WAL mode on a non-synced local path, so a raw file copy of it is
never safe. Snapshots are produced with ``VACUUM INTO``: one consistent file, safe to drop
in a cloud-synced ``[paths].snapshot_dir``. Restore is the mirror image - validate the
snapshot, bank a rescue copy of the index it replaces, clear the replaced file's stale
sidecars, copy the snapshot into place, migrate forward, and report row counts.

Metadata only: nothing here reads, moves, or writes a file under ``[paths].root``
(docs/Architecture.md §6). The only destructive step is on the index itself, which is why
validation precedes it and the rescue copy makes it reversible.
"""

from __future__ import annotations

import fnmatch
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import db

SNAPSHOT_PREFIX = "filingcabinet-"
SNAPSHOT_GLOB = f"{SNAPSHOT_PREFIX}*.db"
SNAPSHOT_TS_FORMAT = "%Y%m%dT%H%M%SZ"

# Rotation policy: every snapshot inside the daily window survives; beyond it only the
# newest snapshot of each ISO week, up to the weekly window.
DAILY_RETENTION_DAYS = 14
WEEKLY_RETENTION_WEEKS = 12

# Sidecars SQLite leaves beside a WAL-mode database.
SIDECAR_SUFFIXES = ("-wal", "-shm")

RESCUE_DIRNAME = "rescue"
# How many same-stamp rescue names to try before giving up (see _claim_rescue_path).
RESCUE_COLLISION_LIMIT = 100


class SnapshotError(RuntimeError):
    """A snapshot could not be written, or is not fit to restore from."""


def _utcnow(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def snapshot_name(when: datetime) -> str:
    return f"{SNAPSHOT_PREFIX}{when.strftime(SNAPSHOT_TS_FORMAT)}.db"


def parse_snapshot_time(path: str | Path) -> datetime | None:
    """UTC timestamp encoded in a snapshot filename, or ``None`` for a foreign name.

    Foreign files in ``snapshot_dir`` (a synced folder may hold anything) parse to
    ``None`` and are therefore never listed and never rotated away.
    """
    name = Path(path).name
    if not fnmatch.fnmatch(name, SNAPSHOT_GLOB):
        return None
    stamp = name[len(SNAPSHOT_PREFIX) : -len(".db")]
    try:
        return datetime.strptime(stamp, SNAPSHOT_TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_snapshots(snapshot_dir: str | Path) -> list[Path]:
    """Snapshots in ``snapshot_dir``, newest first. Unparseable names are ignored."""
    directory = Path(snapshot_dir)
    if not directory.is_dir():
        return []
    dated = [(ts, p) for p in directory.glob(SNAPSHOT_GLOB) if (ts := parse_snapshot_time(p))]
    return [p for _, p in sorted(dated, key=lambda pair: (pair[0], pair[1].name), reverse=True)]


def create_snapshot(
    db_path: str | Path, snapshot_dir: str | Path, *, now: datetime | None = None
) -> Path:
    """Write a timestamped ``VACUUM INTO`` copy of the live index. Returns its path."""
    db_path = Path(db_path)
    if not db.database_exists(db_path):
        raise SnapshotError(f"no database at {db_path}")
    directory = Path(snapshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / snapshot_name(_utcnow(now))
    if target.exists():
        raise SnapshotError(f"snapshot {target} already exists - retry in a moment")
    conn = db.connect(db_path)
    try:
        db.require_migrated(conn)
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        raise SnapshotError(f"could not snapshot {db_path}: {exc}") from exc
    finally:
        conn.close()
    return target


def rotate(snapshot_dir: str | Path, *, now: datetime | None = None) -> list[Path]:
    """Apply the retention policy to ``snapshot_dir``. Returns the deleted paths.

    Keeps everything inside the daily window, the newest snapshot per ISO week inside the
    weekly window, and always the newest snapshot regardless of age.
    """
    snapshots = list_snapshots(snapshot_dir)
    if not snapshots:
        return []
    moment = _utcnow(now)
    daily_cutoff = moment - timedelta(days=DAILY_RETENTION_DAYS)
    weekly_cutoff = moment - timedelta(weeks=WEEKLY_RETENTION_WEEKS)

    keep = {snapshots[0]}  # never delete the newest, however old it is
    seen_weeks: set[tuple[int, int]] = set()
    for path in snapshots:  # newest first, so the first hit per week is that week's newest
        stamp = parse_snapshot_time(path)
        if stamp is None:  # pragma: no cover - list_snapshots filters these out
            continue
        if stamp >= daily_cutoff:
            keep.add(path)
        elif stamp >= weekly_cutoff:
            week = stamp.isocalendar()[:2]
            if week not in seen_weeks:
                seen_weeks.add(week)
                keep.add(path)

    deleted = [p for p in snapshots if p not in keep]
    for path in deleted:
        path.unlink(missing_ok=True)
    return deleted


def _read_only_uri(path: Path) -> str:
    """SQLite read-only URI for ``path``.

    ``as_posix`` keeps the Windows drive letter in the path component (``file:C:/...``),
    which is what SQLite expects; an authority-style ``file:///C:/...`` is not portable.
    """
    return "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"


def validate_snapshot(path: str | Path) -> dict:
    """Check that ``path`` is a readable, migrated index. Raises :class:`SnapshotError`."""
    source = Path(path)
    if not source.is_file():
        raise SnapshotError(f"no snapshot file at {source}")
    conn = None
    try:
        conn = sqlite3.connect(_read_only_uri(source), uri=True)
        conn.row_factory = sqlite3.Row
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SnapshotError(f"snapshot {source} failed integrity_check: {integrity}")
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted({"schema_migrations", "document"} - tables)
        if missing:
            raise SnapshotError(
                f"snapshot {source} is not a migrated index - missing table(s): "
                + ", ".join(missing)
            )
        applied = sorted(
            row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
        )
    except sqlite3.Error as exc:
        raise SnapshotError(f"snapshot {source} is not a readable database: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    return {"path": str(source), "applied": applied, "integrity": "ok"}


def _quote_identifier(name: str) -> str:
    """SQLite-quote a table name from ``sqlite_master`` (embedded quotes doubled)."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """``COUNT(*)`` per user table, by table name."""
    names = sorted(
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    return {
        name: conn.execute(f"SELECT COUNT(*) AS n FROM {_quote_identifier(name)}").fetchone()["n"]
        for name in names
    }


def _same_file(left: Path, right: Path) -> bool:
    """True if both paths name the same file, whether or not they both exist.

    ``samefile`` is the accurate answer (it sees hard links and junctions) but needs both
    files present; ``resolve`` covers the rest, including the destination-missing case.
    """
    try:
        if left.samefile(right):
            return True
    except OSError:
        pass
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - resolve is non-strict; guard the exotic cases
        return False


def sidecar_paths(db_path: str | Path) -> list[Path]:
    return [Path(str(db_path) + suffix) for suffix in SIDECAR_SUFFIXES]


def _claim_rescue_path(rescue_dir: Path, db_path: Path, stamp: str) -> Path:
    """Create and return an unused ``<stem>-pre-restore-<stamp>[-N].db`` in ``rescue_dir``.

    The rescue copy is the only undo ``restore`` offers, so it must never land on an
    existing file: two restores in the same wall-clock second share a timestamp, and the
    second would otherwise overwrite the first one's bank - including the case where that
    bank *is* the snapshot being restored from. The file is created exclusively (not merely
    checked for absence) so a concurrent restore cannot claim the same name.
    """
    base = f"{db_path.stem}-pre-restore-{stamp}"
    for index in range(RESCUE_COLLISION_LIMIT):
        suffix = "" if index == 0 else f"-{index + 1}"
        candidate = rescue_dir / f"{base}{suffix}.db"
        try:
            candidate.touch(exist_ok=False)  # claim the name; copy2 fills it in
        except FileExistsError:
            continue
        return candidate
    raise SnapshotError(
        f"could not bank a rescue copy in {rescue_dir}: "
        f"{RESCUE_COLLISION_LIMIT} names already taken for {base}"
    )


def restore_snapshot(
    db_path: str | Path,
    source: str | Path,
    *,
    now: datetime | None = None,
    migrations_dir: Path = db.DEFAULT_MIGRATIONS_DIR,
) -> dict:
    """Replace the live index with ``source``. Validation first, rescue copy second.

    No connection to the live index is open while its sidecars are cleared - deleting a
    ``-wal`` out from under an open connection loses committed data.
    """
    db_path = Path(db_path)
    source = Path(source)
    validate_snapshot(source)  # abort before touching anything
    if _same_file(source, db_path):
        raise SnapshotError(
            f"refusing to restore {db_path} from itself: {source} is the live index, "
            "not a snapshot - restore needs a separate file (try `restore latest`)"
        )

    rescue_copy: Path | None = None
    if db.database_exists(db_path):
        rescue_dir = db_path.parent / RESCUE_DIRNAME
        rescue_dir.mkdir(parents=True, exist_ok=True)
        stamp = _utcnow(now).strftime(SNAPSHOT_TS_FORMAT)
        rescue_copy = _claim_rescue_path(rescue_dir, db_path, stamp)
        shutil.copy2(db_path, rescue_copy)

    cleared: list[str] = []
    for sidecar in sidecar_paths(db_path):
        if sidecar.exists():
            sidecar.unlink()
            cleared.append(sidecar.name)
    db_path.unlink(missing_ok=True)

    # Past this point the live index is gone, so every failure is reported as a
    # SnapshotError naming the rescue copy - the user's undo - instead of a raw traceback.
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, db_path)  # copy, never move: the snapshot stays in snapshot_dir

        conn = db.connect(db_path)
        try:
            applied = db.migrate(conn, migrations_dir)
            counts = row_counts(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        undo = f"; the replaced index is banked at {rescue_copy}" if rescue_copy else ""
        raise SnapshotError(
            f"restore of {db_path} from {source} failed after the old index was "
            f"cleared: {exc}{undo}"
        ) from exc

    return {
        "db": str(db_path),
        "restored_from": str(source),
        "rescue_copy": str(rescue_copy) if rescue_copy else None,
        "cleared_sidecars": cleared,
        "applied": applied,
        "row_counts": counts,
    }
