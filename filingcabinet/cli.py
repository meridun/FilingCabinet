"""`filingcabinet` (alias `fc`) command-line entry point.

Verbs: migrate, status, ingest, snapshot, restore, instance.

DB path resolution: --db flag > FC_DB env var > config.toml [paths].data_dir + /filingcabinet.db.
Config path resolution: --config flag > FC_CONFIG env var > ./config.toml.
Every verb supports --json so agents (and the phase-7 MCP wrapper) get structured output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from . import (
    __version__,
    db,
    ingest as ingest_mod,
    instance as instance_mod,
    snapshot as snapshot_mod,
)

DB_FILENAME = "filingcabinet.db"


def load_config(config_arg: str | None) -> dict:
    config_path = Path(config_arg or os.environ.get("FC_CONFIG") or "config.toml")
    if config_path.is_file():
        with config_path.open("rb") as fh:
            return tomllib.load(fh)
    return {}


def resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    env_db = os.environ.get("FC_DB")
    if env_db:
        return Path(env_db)
    config = load_config(args.config)
    data_dir = config.get("paths", {}).get("data_dir")
    if data_dir:
        return Path(data_dir) / DB_FILENAME
    raise SystemExit(
        "error: no database path - pass --db, set FC_DB, or set [paths].data_dir in "
        "config.toml (see config.example.toml)"
    )


def resolve_root(args: argparse.Namespace) -> Path:
    """Document root resolution: --root flag > FC_ROOT env var > config.toml [paths].root."""
    root = getattr(args, "root", None) or os.environ.get("FC_ROOT")
    if not root:
        config = load_config(args.config)
        root = config.get("paths", {}).get("root")
    if not root:
        raise SystemExit(
            "error: no document root - pass --root, set FC_ROOT, or set [paths].root in "
            "config.toml (see config.example.toml)"
        )
    path = Path(root)
    if not path.is_dir():
        raise SystemExit(f"error: document root {path} is not a directory")
    return path


def resolve_snapshot_dir(args: argparse.Namespace) -> Path:
    """Snapshot dir: --snapshot-dir flag > FC_SNAPSHOT_DIR env > config [paths].snapshot_dir."""
    snapshot_dir = getattr(args, "snapshot_dir", None) or os.environ.get("FC_SNAPSHOT_DIR")
    if not snapshot_dir:
        config = load_config(args.config)
        snapshot_dir = config.get("paths", {}).get("snapshot_dir")
    if not snapshot_dir:
        raise SystemExit(
            "error: no snapshot directory - pass --snapshot-dir, set FC_SNAPSHOT_DIR, or set "
            "[paths].snapshot_dir in config.toml (see config.example.toml)"
        )
    return Path(snapshot_dir)


def _emit(args: argparse.Namespace, payload: dict, text: str) -> None:
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(text)


def cmd_migrate(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    exists = db.database_exists(db_path)
    if not exists and not args.create:
        raise SystemExit(
            f"error: no database at {db_path} - pass --create to bootstrap a new index "
            "(plain `migrate` never creates one)"
        )
    conn = db.connect(db_path)
    applied = db.migrate(conn)
    conn.close()
    verb = "created" if not exists else "migrated"
    detail = ": " + ", ".join(applied) if applied else ""
    _emit(
        args,
        {"db": str(db_path), "created": not exists, "applied": applied},
        f"{verb} {db_path}: applied {len(applied)} migration(s){detail}",
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not db.database_exists(db_path):
        raise SystemExit(f"error: no database at {db_path} - run `filingcabinet migrate --create`")
    conn = db.connect(db_path)
    migrated = db.is_migrated(conn)
    pending = [p.name for p in db.pending_migrations(conn)]
    docs = conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] if migrated else 0
    conn.close()
    state = "migrated" if migrated else "NOT migrated"
    _emit(
        args,
        {"db": str(db_path), "migrated": migrated, "pending": pending, "documents": docs},
        f"{db_path}: {state}, {len(pending)} pending, {docs} document(s)",
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not db.database_exists(db_path):
        raise SystemExit(f"error: no database at {db_path} - run `filingcabinet migrate --create`")
    root = resolve_root(args)
    ingest_cfg = load_config(args.config).get("ingest", {})
    extensions = ingest_cfg.get("extensions")
    excludes = ingest_cfg.get("exclude")
    conn = db.connect(db_path)
    try:
        summary = ingest_mod.run_ingest(
            conn,
            root,
            extensions=frozenset(e.lower() for e in extensions) if extensions else None,
            excludes=tuple(excludes) if excludes else None,
        )
    finally:
        conn.close()
    payload = {"db": str(db_path), "root": str(root), **summary.as_dict()}
    _emit(
        args,
        payload,
        f"{root}: scanned {summary.scanned}, {summary.new} new, {summary.changed} changed, "
        f"{summary.unchanged} unchanged, {summary.missing} missing, {summary.errors} error(s)",
    )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    if not db.database_exists(db_path):
        raise SystemExit(f"error: no database at {db_path} - run `filingcabinet migrate --create`")
    snapshot_dir = resolve_snapshot_dir(args)
    target = snapshot_mod.create_snapshot(db_path, snapshot_dir)
    rotated = [] if args.no_rotate else snapshot_mod.rotate(snapshot_dir)
    _emit(
        args,
        {
            "db": str(db_path),
            "snapshot": str(target),
            "snapshot_dir": str(snapshot_dir),
            "rotated": [str(p) for p in rotated],
        },
        f"{target}: snapshot written, {len(rotated)} rotated",
    )
    return 0


def _resolve_restore_source(args: argparse.Namespace) -> Path:
    if args.target == "latest":
        snapshot_dir = resolve_snapshot_dir(args)
        snapshots = snapshot_mod.list_snapshots(snapshot_dir)
        if not snapshots:
            raise SystemExit(
                f"error: no snapshots in {snapshot_dir} - run `filingcabinet snapshot` first"
            )
        return snapshots[0]
    source = Path(args.target)
    if not source.is_file():
        raise SystemExit(f"error: no snapshot file at {source}")
    return source


def cmd_restore(args: argparse.Namespace) -> int:
    """Replace the live index with a snapshot. Destructive to the index only, never to docs."""
    db_path = resolve_db_path(args)
    source = _resolve_restore_source(args)
    if args.dry_run:
        report = snapshot_mod.validate_snapshot(source)
        _emit(
            args,
            {"db": str(db_path), "dry_run": True, **report},
            f"{source}: valid snapshot ({len(report['applied'])} migration(s) applied); "
            f"would replace {db_path}",
        )
        return 0
    result = snapshot_mod.restore_snapshot(db_path, source)
    counts = result["row_counts"]
    detail = ", ".join(f"{name}={n}" for name, n in counts.items()) or "no tables"
    _emit(
        args,
        result,
        f"{db_path}: restored from {source}, applied {len(result['applied'])} migration(s); "
        f"{detail}",
    )
    return 0


def cmd_instance_init(args: argparse.Namespace) -> int:
    try:
        result = instance_mod.init_instance(Path(args.dir), force=args.force)
    except NotADirectoryError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"error: cannot scaffold instance at {args.dir}: {exc}") from exc
    _emit(
        args,
        result,
        f"initialized instance at {result['path']}: "
        f"{len(result['created'])} created, {len(result['skipped'])} skipped",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="filingcabinet", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"filingcabinet {__version__}")
    parser.add_argument("--db", help="index database path (overrides FC_DB and config)")
    parser.add_argument("--config", help="config.toml path (overrides FC_CONFIG)")
    parser.add_argument(
        "--snapshot-dir", help="snapshot directory (overrides FC_SNAPSHOT_DIR and config)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply pending migrations")
    p.add_argument("--create", action="store_true", help="bootstrap a new index database")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("status", help="index summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("ingest", help="scan the document root and index new or changed files")
    p.add_argument("--root", help="document root (overrides FC_ROOT and config)")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("snapshot", help="write a VACUUM INTO snapshot of the index")
    p.add_argument("--no-rotate", action="store_true", help="keep every existing snapshot")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("restore", help="replace the live index with a snapshot")
    p.add_argument("target", help="`latest` or the path to a snapshot file")
    p.add_argument("--dry-run", action="store_true", help="validate and report, change nothing")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("instance", help="manage the private instance repo")
    instance_sub = p.add_subparsers(dest="instance_command", required=True)
    q = instance_sub.add_parser("init", help="scaffold a new instance directory")
    q.add_argument("dir", help="target directory for the instance")
    q.add_argument("--force", action="store_true", help="overwrite existing scaffold files")
    q.set_defaults(func=cmd_instance_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (db.NotMigratedError, snapshot_mod.SnapshotError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
