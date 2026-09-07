"""Connection handling and migrations runner.

Pragmas on every connect: WAL journal mode and foreign_keys=ON. Migrations are plain
numbered .sql files in migrations/, applied in lexicographic order and tracked in
schema_migrations. Forward-only by design.

The live index DB must sit on a non-synced local path: cloud sync clients corrupt the
-wal/-shm sidecars mid-write. `VACUUM INTO` snapshots are the thing that syncs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# A core table from 001_init used to detect an un-migrated database.
_SENTINEL_TABLE = "document"


class NotMigratedError(RuntimeError):
    def __init__(self, message: str = "database not migrated - run `filingcabinet migrate` first"):
        super().__init__(message)


def database_exists(db_path: str | Path) -> bool:
    """True if ``db_path`` names a real, non-empty database file.

    ``sqlite3.connect`` creates the file eagerly and leaves a zero-byte file behind when
    nothing is written, so size matters, not just presence. ``:memory:`` always exists.
    """
    if str(db_path) == ":memory:":
        return True
    path = Path(db_path)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the required pragmas.

    Creates on connect: this is the low-level primitive. The refuse-to-create gate lives
    in the CLI entry points (:func:`database_exists`).
    """
    db_path = Path(db_path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version     TEXT PRIMARY KEY,
          applied_at  TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    _ensure_migrations_table(conn)
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def pending_migrations(
    conn: sqlite3.Connection, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[Path]:
    applied = applied_versions(conn)
    return [p for p in sorted(migrations_dir.glob("*.sql")) if p.name not in applied]


def migrate(conn: sqlite3.Connection, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration in order. Returns the versions applied."""
    applied: list[str] = []
    for path in pending_migrations(conn, migrations_dir):
        with conn:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (path.name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        applied.append(path.name)
    return applied


def is_migrated(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_SENTINEL_TABLE,)
    ).fetchone()
    return row is not None


def require_migrated(conn: sqlite3.Connection) -> None:
    if not is_migrated(conn):
        raise NotMigratedError()
