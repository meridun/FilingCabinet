"""Phase 6 plan execution: `apply`, the move log, and `undo` (docs/Architecture.md §6).

The one module in the package that renames a file under ``[paths].root``. Everything it does is
driven by an *explicit* plan file a human passed on the command line: it never calls
:func:`organize.build_plan`, never re-derives a proposal, and never touches a file the plan did
not already list with ``status == 'move'``. That is the shape of the project invariant "tools
never move or rename documents unasked".

The plan file is **untrusted, human-editable input**. ``entries[].target_path`` is the only value
acted on - a name is never re-rendered from ``entries[].fields``, whose ``party`` / ``detail`` /
``doc_date`` originate in OCR text (attacker-influenceable content inside a scanned document) -
and the containment check (``resolved.is_relative_to(root.resolve())``) is re-run per entry
immediately before the write, because `propose`'s own check is time-of-check and `apply` runs
later, against a filesystem that may have changed underneath it. The plan's ``root`` must also
match the root the CLI resolves, so a plan file cannot retarget the tool at another tree.

Ordering per move is **write-ahead**: the ``move_log`` row is committed *before* the irreversible
rename, so no move can ever be unlogged - and unlogged means unreversible, which §6 forbids. The
inverse failure, a committed row whose rename then failed, is visible and benign: the row is
deleted on an in-process failure, and a crash-orphan is reported by `undo` as "target no longer
on disk" and reconciled by the next `ingest`.

Every entry is independent: a skip is a report, never an abort. Commits are per entry (the same
contract `ingest.run_ingest` keeps - an interrupted run is resumable, and one bad entry never
rolls back an earlier good move).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db, organize


class PlanError(RuntimeError):
    """An unusable plan file: unreadable, malformed, or of an unsupported ``plan_version``."""


STATUS_MOVED = "moved"
STATUS_SKIPPED = "skipped"
STATUS_IGNORED = "ignored"
STATUS_REVERSED = "reversed"

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class MoveOutcome:
    """What happened to one plan entry (or one ``move_log`` row, for `undo`)."""

    document_id: int
    from_path: str
    to_path: str
    status: str
    reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ApplySummary:
    """Run counts, the reporting shape `propose` already prints from ``organize.PlanSummary``."""

    plan_id: str
    entries: int = 0
    moved: int = 0
    reversed: int = 0
    skipped: int = 0
    ignored: int = 0
    errors: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def read_plan(path: str | Path) -> dict:
    """Load and validate a plan file. Raises :class:`PlanError` on anything unusable.

    Shape only - the security-relevant re-validation of each entry happens per entry in
    :func:`apply_plan`, immediately before the write.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"cannot read plan {path}: {exc.strerror or exc}") from exc
    try:
        plan = json.loads(raw)
    except ValueError as exc:
        raise PlanError(f"plan {path} is not valid JSON: {exc}") from exc
    if not isinstance(plan, Mapping):
        raise PlanError(f"plan {path} is not a JSON object")
    version = plan.get("plan_version")
    if version != organize.PLAN_VERSION:
        raise PlanError(
            f"plan {path} has plan_version {version!r}; this build reads "
            f"{organize.PLAN_VERSION} - re-run `propose` to rebuild it"
        )
    plan_id = plan.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise PlanError(f"plan {path} has no plan_id")
    if not isinstance(plan.get("entries"), list):
        raise PlanError(f"plan {path} has no entries list")
    return dict(plan)


def _resolve_under_root(root: Path, rel: str | None) -> Path | None:
    """The absolute path ``rel`` names under ``root``, or ``None`` when it is not inside it.

    Absolute, drive-qualified, and traversing values are rejected before resolution; the
    resolved result must still be strictly inside the root (the root itself is not a document).
    """
    if not isinstance(rel, str) or not rel.strip():
        return None
    if rel.startswith(("/", "\\")) or _DRIVE_RE.match(rel) or Path(rel).is_absolute():
        return None
    try:
        resolved_root = root.resolve()
        candidate = resolved_root / rel
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        return None
    # Containment is judged on the fully resolved path, but the final component keeps the plan's
    # own spelling: on Windows ``resolve()`` rewrites an existing name to its on-disk case, which
    # would silently turn a case-only rename (`foo.pdf` -> `Foo.pdf`) into a no-op.
    return resolved.parent / candidate.name


def _stability_reason(path: Path, mtime: float | None, size: int | None) -> str | None:
    """``None`` when the file is exactly what the plan recorded, else the reason to skip it.

    The cheap proxy agreed for concurrency with the sync client: the (mtime, size) pair plus a
    read-open probe, mirroring `ingest`'s incremental-skip contract. Nothing is re-hashed here.
    """
    try:
        stat = path.stat()
    except OSError as exc:
        return f"cannot stat {path.name}: {exc.strerror or exc}"
    if not path.is_file():
        return f"{path.name} is not a regular file"
    if size is not None and int(stat.st_size) != int(size):
        return f"size changed since the plan was built ({size} -> {stat.st_size})"
    if mtime is not None and float(stat.st_mtime) != float(mtime):
        return f"mtime changed since the plan was built ({mtime} -> {stat.st_mtime})"
    try:  # the sync client holds a lock mid-write: the same failure `run_ingest` tolerates
        with path.open("rb"):
            pass
    except OSError as exc:
        return f"locked or unreadable: {exc.strerror or exc}"
    return None


def _is_case_only(src: Path, dest: Path) -> bool:
    """A rename that differs only in case - not a collision on the case-insensitive host."""
    return src.parent == dest.parent and src.name.casefold() == dest.name.casefold()


def _move(src: Path, dest: Path) -> None:
    """Rename ``src`` to ``dest``, creating the destination folder.

    ``os.rename``, never ``os.replace``: replace clobbers an existing file silently and a
    document is never worth overwriting. Windows refuses to clobber outright; on POSIX a
    residual TOCTOU window remains between the caller's existence check and this call, accepted
    for a single-user corpus.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dest)


def _document_fields(fields: object) -> tuple[str | None, str | None, str | None, str | None]:
    """``(doc_date, party, doc_type, detail)`` from the plan's raw ``fields`` map.

    These are the pre-sanitization, OCR-derived values. They are bound as SQL parameters and
    never interpolated, and are never used to build a path - only ``target_path`` is. A
    ``doc_date`` that is not a plain ISO date is dropped rather than stored.
    """
    mapping = fields if isinstance(fields, Mapping) else {}

    def clean(key: str) -> str | None:
        value = mapping.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    doc_date = clean("doc_date")
    if doc_date is not None and not _ISO_DATE_RE.fullmatch(doc_date):
        doc_date = None
    return doc_date, clean("party"), clean("doc_type"), clean("detail")


def _reindex(
    conn: sqlite3.Connection,
    document_id: int,
    from_path: str,
    to_path: str,
    landed: Path,
    fields: object,
    stamp: str,
) -> str | None:
    """Point the index at the moved file and commit the plan's fields.

    Never fatal: the file has already moved and the ``move_log`` row is truth, so index drift is
    appended to the outcome's reason and left for the next `ingest` to reconcile.
    """
    try:
        stat = landed.stat()
        mtime: float | None = stat.st_mtime
        size: int | None = stat.st_size
    except OSError:  # pragma: no cover - the file was just renamed into place
        mtime = size = None

    reason = None
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE occurrence SET rel_path = ?, mtime = COALESCE(?, mtime), "
                "size_bytes = COALESCE(?, size_bytes) "
                "WHERE document_id = ? AND rel_path = ? AND missing_since IS NULL",
                (to_path, mtime, size, document_id, from_path),
            )
        if cursor.rowcount == 0:
            reason = "index not updated: no live occurrence at the old path; `ingest` reconciles"
    except sqlite3.IntegrityError:
        reason = f"index not updated: {to_path} is already indexed; `ingest` reconciles"

    with conn:
        conn.execute(
            "UPDATE document SET doc_date = ?, party = ?, doc_type = ?, detail = ?, "
            "updated_at = ? WHERE document_id = ?",
            (*_document_fields(fields), stamp, document_id),
        )
    return reason


def apply_plan(
    conn: sqlite3.Connection,
    plan: Mapping,
    *,
    root: Path,
    dry_run: bool = False,
    now: str | None = None,
) -> tuple[list[MoveOutcome], ApplySummary]:
    """Execute the ``status == 'move'`` entries of ``plan``, in plan order.

    Only entries the plan already marked ``move`` are acted on; ``noop`` / ``unclassified`` /
    ``collision`` / ``error`` are ignored and never retried. A skipped entry does not abort the
    batch.
    """
    db.require_migrated(conn)
    root = Path(root)
    plan_id = str(plan.get("plan_id") or "")
    entries = plan.get("entries") or []
    stamp = now or _now()

    outcomes: list[MoveOutcome] = []
    errors = 0

    def record(
        document_id: object, from_path: str, to_path: str, status: str, reason: str | None = None
    ) -> None:
        try:
            identifier = int(document_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            identifier = 0
        outcomes.append(MoveOutcome(identifier, from_path, to_path, status, reason))

    for entry in entries:
        if not isinstance(entry, Mapping):
            record(0, "", "", STATUS_IGNORED, "malformed plan entry")
            continue
        document_id = entry.get("document_id")
        from_path = str(entry.get("current_path") or "")
        to_path = str(entry.get("target_path") or "")
        status = entry.get("status")

        if status != organize.STATUS_MOVE:
            record(document_id, from_path, to_path, STATUS_IGNORED, f"plan status: {status}")
            continue
        try:
            document_id = int(document_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            record(document_id, from_path, to_path, STATUS_IGNORED, "entry has no document_id")
            continue

        # Re-run containment on both sides, now, against the live filesystem: `propose`'s check
        # was time-of-check and a plan file is hand-editable (docs/Architecture.md §6).
        source = _resolve_under_root(root, from_path)
        target = _resolve_under_root(root, to_path)
        if source is None or target is None:
            side = "current_path" if source is None else "target_path"
            record(
                document_id, from_path, to_path, STATUS_SKIPPED,
                f"{side} is outside the document root",
            )
            continue

        unstable = _stability_reason(source, entry.get("current_mtime"), entry.get("current_size"))
        if unstable is not None:
            record(document_id, from_path, to_path, STATUS_SKIPPED, unstable)
            continue
        if target.exists() and not _is_case_only(source, target):
            record(document_id, from_path, to_path, STATUS_SKIPPED, "target already exists")
            continue
        if dry_run:
            record(document_id, from_path, to_path, STATUS_MOVED)
            continue

        with conn:  # write-ahead: logged before the irreversible rename, never after
            cursor = conn.execute(
                "INSERT INTO move_log (document_id, plan_id, from_path, to_path, applied_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (document_id, plan_id, from_path, to_path, stamp),
            )
        move_id = int(cursor.lastrowid)
        try:
            _move(source, target)
        except OSError as exc:
            with conn:  # the rename never happened: drop the row rather than leave it lying
                conn.execute("DELETE FROM move_log WHERE move_id = ?", (move_id,))
            errors += 1
            record(
                document_id, from_path, to_path, STATUS_SKIPPED,
                f"move failed: {exc.strerror or exc}",
            )
            continue
        reason = _reindex(
            conn, document_id, from_path, to_path, target, entry.get("fields"), stamp
        )
        record(document_id, from_path, to_path, STATUS_MOVED, reason)

    return outcomes, _summarize(plan_id, len(entries), outcomes, errors, dry_run)


def undo_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    root: Path,
    dry_run: bool = False,
    now: str | None = None,
) -> tuple[list[MoveOutcome], ApplySummary]:
    """Reverse every un-undone ``move_log`` row of ``plan_id``, newest first.

    Reverse order so folder-creating moves unwind cleanly. An empty row set is a report, not an
    error: re-running `undo` on an already-undone plan is a no-op.
    """
    db.require_migrated(conn)
    root = Path(root)
    plan_id = str(plan_id)
    stamp = now or _now()
    rows = conn.execute(
        "SELECT move_id, document_id, from_path, to_path FROM move_log "
        "WHERE plan_id = ? AND undone_at IS NULL ORDER BY move_id DESC",
        (plan_id,),
    ).fetchall()

    outcomes: list[MoveOutcome] = []
    errors = 0

    # An outcome names the movement *this* operation performs, so a reversal reads
    # from the log row's to_path back to its from_path.
    def record(row, status: str, reason: str | None = None) -> None:
        outcomes.append(
            MoveOutcome(
                int(row["document_id"]), str(row["to_path"]), str(row["from_path"]), status, reason
            )
        )

    for row in rows:
        move_id = int(row["move_id"])
        document_id = int(row["document_id"])
        from_path = str(row["from_path"])
        to_path = str(row["to_path"])

        source = _resolve_under_root(root, to_path)  # where the file is now
        target = _resolve_under_root(root, from_path)  # where it goes back to
        if source is None or target is None:
            record(row, STATUS_SKIPPED, "logged path is outside the document root")
            continue
        if not source.exists():
            record(row, STATUS_SKIPPED, "target no longer on disk")
            continue

        # The baseline is the pair `apply` wrote onto the occurrence row. Where the index has
        # since drifted there is nothing to compare, and the lock probe alone applies.
        occurrence = conn.execute(
            "SELECT mtime, size_bytes FROM occurrence WHERE rel_path = ? AND document_id = ?",
            (to_path, document_id),
        ).fetchone()
        if occurrence is None:
            note, baseline = "no indexed baseline; lock probe only", (None, None)
        else:
            note, baseline = None, (occurrence["mtime"], occurrence["size_bytes"])
        unstable = _stability_reason(source, *baseline)
        if unstable is not None:
            record(row, STATUS_SKIPPED, unstable)
            continue
        if target.exists() and not _is_case_only(source, target):
            record(row, STATUS_SKIPPED, "the original path is occupied")
            continue
        if dry_run:
            record(row, STATUS_REVERSED, note)
            continue

        try:
            _move(source, target)
        except OSError as exc:
            errors += 1
            record(row, STATUS_SKIPPED, f"reverse failed: {exc.strerror or exc}")
            continue
        with conn:  # the log first, as in `apply`: a crash mid-undo is reported, never silent
            conn.execute("UPDATE move_log SET undone_at = ? WHERE move_id = ?", (stamp, move_id))
        try:
            stat = target.stat()
            mtime: float | None = stat.st_mtime
            size: int | None = stat.st_size
        except OSError:  # pragma: no cover - the file was just renamed into place
            mtime = size = None
        try:
            with conn:
                conn.execute(
                    "UPDATE occurrence SET rel_path = ?, mtime = COALESCE(?, mtime), "
                    "size_bytes = COALESCE(?, size_bytes) "
                    "WHERE document_id = ? AND rel_path = ?",
                    (from_path, mtime, size, document_id, to_path),
                )
        except sqlite3.IntegrityError:
            note = f"index not updated: {from_path} is already indexed; `ingest` reconciles"
        record(row, STATUS_REVERSED, note)

    return outcomes, _summarize(plan_id, len(rows), outcomes, errors, dry_run)


def _summarize(
    plan_id: str, entries: int, outcomes: list[MoveOutcome], errors: int, dry_run: bool
) -> ApplySummary:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return ApplySummary(
        plan_id=plan_id,
        entries=entries,
        moved=counts.get(STATUS_MOVED, 0),
        reversed=counts.get(STATUS_REVERSED, 0),
        skipped=counts.get(STATUS_SKIPPED, 0),
        ignored=counts.get(STATUS_IGNORED, 0),
        errors=errors,
        dry_run=bool(dry_run),
    )
