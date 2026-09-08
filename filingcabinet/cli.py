"""`filingcabinet` (alias `fc`) command-line entry point.

Verbs: migrate, status, ingest, ocr, find, doctor, dupes, snapshot, restore, instance.

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
    dedup as dedup_mod,
    ingest as ingest_mod,
    instance as instance_mod,
    ocr as ocr_mod,
    search as search_mod,
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


def _require_database(args: argparse.Namespace) -> Path:
    db_path = resolve_db_path(args)
    if not db.database_exists(db_path):
        raise SystemExit(f"error: no database at {db_path} - run `filingcabinet migrate --create`")
    return db_path


def _resolve_max_distance(args: argparse.Namespace) -> int:
    """Threshold precedence: --max-distance > [dedup].phash_max_distance > built-in default."""
    if args.max_distance is not None:
        return int(args.max_distance)
    configured = load_config(args.config).get("dedup", {}).get("phash_max_distance")
    if configured is None:
        return dedup_mod.DEFAULT_PHASH_MAX_DISTANCE
    try:  # a bad config value is a CLI error, not a traceback
        return int(configured)
    except (TypeError, ValueError):
        raise SystemExit(
            f"error: [dedup].phash_max_distance must be an integer, got {configured!r}"
        ) from None


def cmd_dupes_report(args: argparse.Namespace) -> int:
    db_path = _require_database(args)
    root = resolve_root(args)
    max_distance = _resolve_max_distance(args)
    conn = db.connect(db_path)
    try:
        summary = dedup_mod.run_report(conn, root, max_distance=max_distance)
    finally:
        conn.close()
    payload = {
        "db": str(db_path),
        "root": str(root),
        "max_distance": max_distance,
        **summary.as_dict(),
    }
    _emit(
        args,
        payload,
        f"{root}: {summary.exact_groups} exact group(s), {summary.near} near, "
        f"{summary.subset} subset, {summary.queued} queued",
    )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    db_path = _require_database(args)
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


def cmd_dupes_label(args: argparse.Namespace) -> int:
    db_path = _require_database(args)
    conn = db.connect(db_path)
    try:
        db.require_migrated(conn)
        if args.export:
            labels = dedup_mod.export_labels(conn)
            payload = {"db": str(db_path), "labels": labels}
            text = "\n".join(
                f"{row['document_a']} {row['document_b']} {row['kind']}: {row['verdict']}"
                for row in labels
            ) or "no labels recorded"
        elif args.pair:
            if not args.kind or not args.verdict:
                raise SystemExit("error: --pair requires --kind and --verdict")
            document_a, document_b = args.pair
            try:
                verdict = dedup_mod.record_label(
                    conn, document_a, document_b, args.kind, args.verdict
                )
            except ValueError as exc:
                raise SystemExit(f"error: {exc}") from exc
            payload = {
                "db": str(db_path),
                "document_a": document_a,
                "document_b": document_b,
                "kind": args.kind,
                "verdict": verdict,
            }
            text = f"labelled {document_a} {document_b} ({args.kind}): {verdict}"
        else:  # --list
            pending = [dict(row) for row in dedup_mod.pending_reviews(conn, limit=args.limit)]
            payload = {"db": str(db_path), "pending": pending}
            text = "\n".join(
                f"#{row['review_id']} {row['kind']} {row['document_a']} {row['document_b']}: "
                f"{row['detail']}"
                for row in pending
            ) or "review queue empty"
    finally:
        conn.close()
    _emit(args, payload, text)
    return 0


def _resolve_ocr_config(args: argparse.Namespace) -> ocr_mod.OcrConfig:
    """Ladder and threshold from `[ocr]`; a missing or malformed table falls back to defaults."""
    return ocr_mod.OcrConfig.from_mapping(load_config(args.config).get("ocr"))


def cmd_ocr_run(args: argparse.Namespace) -> int:
    db_path = _require_database(args)
    root = resolve_root(args)
    config = _resolve_ocr_config(args)
    conn = db.connect(db_path)
    try:
        summary = ocr_mod.run_ocr(conn, root, config=config, limit=args.limit)
    finally:
        conn.close()
    payload = {
        "db": str(db_path),
        "root": str(root),
        "ladder": list(config.ladder),
        "min_confidence": config.min_confidence,
        **summary.as_dict(),
    }
    _emit(
        args,
        payload,
        f"{root}: {summary.documents} document(s), {summary.pages} page(s) - {summary.ok} ok, "
        f"{summary.pending_vision} pending vision, {summary.skipped} skipped, "
        f"{summary.exhausted} exhausted, {summary.errors} error(s)",
    )
    return 0


def _read_submitted_text(args: argparse.Namespace) -> str:
    if args.text_file == "-":
        return sys.stdin.read()
    path = Path(args.text_file)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SystemExit(f"error: cannot read {path}: {exc}") from exc


def cmd_ocr_submit(args: argparse.Namespace) -> int:
    db_path = _require_database(args)
    text = _read_submitted_text(args)
    conn = db.connect(db_path)
    try:
        result = ocr_mod.submit_vision_text(conn, args.document, args.page, text)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    finally:
        conn.close()
    _emit(
        args,
        {"db": str(db_path), **result},
        f"document {result['document_id']} page {result['page']}: "
        f"{result['chars']} character(s) committed as vision",
    )
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    db_path = _require_database(args)
    conn = db.connect(db_path)
    try:
        hits = search_mod.find(conn, args.query, limit=args.limit or search_mod.DEFAULT_LIMIT)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    finally:
        conn.close()
    payload = {"query": args.query, "count": len(hits), "hits": [hit.as_dict() for hit in hits]}
    text = "\n".join(
        f"{hit.rel_path or hit.sha256[:12]} - {hit.snippet}" for hit in hits
    ) or "no matches"
    _emit(args, payload, text)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report the OCR toolchain. Absence is a report, not a failure: always exits 0."""
    tesseract = ocr_mod.tesseract_version()
    pymupdf = ocr_mod.pymupdf_version()
    config = _resolve_ocr_config(args)
    payload = {
        "tesseract": {"present": tesseract is not None, "version": tesseract},
        "pymupdf": {"present": pymupdf is not None, "version": pymupdf},
        "ladder": list(config.ladder),
        "min_confidence": config.min_confidence,
    }
    try:  # the db is optional here - doctor reports the toolchain either way
        db_path = resolve_db_path(args)
    except SystemExit:
        db_path = None
    if db_path is not None and db.database_exists(db_path):
        conn = db.connect(db_path)
        try:
            payload["db"] = str(db_path)
            payload["migrated"] = db.is_migrated(conn)
        finally:
            conn.close()
    elif db_path is not None:
        payload["db"] = str(db_path)
        payload["migrated"] = False
    _emit(
        args,
        payload,
        f"tesseract: {tesseract or 'not found'}\npymupdf: {pymupdf or 'not found'}\n"
        f"ladder: {', '.join(config.ladder)} (min_confidence {config.min_confidence})",
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

    p = sub.add_parser("ocr", help="run the OCR ladder, or commit agent-supplied text")
    ocr_sub = p.add_subparsers(dest="ocr_command", required=True)
    q = ocr_sub.add_parser("run", help="walk the [ocr].ladder over indexed documents")
    q.add_argument("--root", help="document root (overrides FC_ROOT and config)")
    q.add_argument("--limit", type=int, help="cap the number of documents processed")
    q.set_defaults(func=cmd_ocr_run)
    q = ocr_sub.add_parser("submit", help="commit agent-supplied text for one page (vision rung)")
    q.add_argument("--document", type=int, required=True, help="document_id")
    q.add_argument("--page", type=int, required=True, help="1-based page number")
    q.add_argument("--text-file", required=True, help="file holding the page text, or - for stdin")
    q.set_defaults(func=cmd_ocr_submit)

    p = sub.add_parser("find", help="full-text search over OCR text")
    p.add_argument("query", help="search terms (FTS5 syntax is honoured when present)")
    p.add_argument("--limit", type=int, help="maximum hits to return")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("doctor", help="report the OCR toolchain (tesseract, PyMuPDF)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("dupes", help="duplicate detection: report tiers, label the sample")
    dupes_sub = p.add_subparsers(dest="dupes_command", required=True)
    q = dupes_sub.add_parser("report", help="exact, near-duplicate, and subset tiers")
    q.add_argument("--root", help="document root (overrides FC_ROOT and config)")
    q.add_argument(
        "--max-distance",
        type=int,
        help="perceptual-hash Hamming threshold (overrides [dedup].phash_max_distance)",
    )
    q.set_defaults(func=cmd_dupes_report)
    q = dupes_sub.add_parser("label", help="build the labelled sample used to tune thresholds")
    group = q.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="pending review-queue pairs")
    group.add_argument(
        "--pair", nargs=2, type=int, metavar=("A", "B"), help="record a verdict for one pair"
    )
    group.add_argument("--export", action="store_true", help="the labelled sample as records")
    q.add_argument("--kind", choices=dedup_mod.VALID_KINDS, help="tier the pair came from")
    q.add_argument("--verdict", choices=["dup", "not-dup", "not_dup"], help="the human judgment")
    q.add_argument("--limit", type=int, help="cap the number of pending pairs listed")
    q.set_defaults(func=cmd_dupes_label)

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
