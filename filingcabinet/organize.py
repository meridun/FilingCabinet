"""Phase 5 plan generation: name rendering, path safety, collisions, the plan file
(docs/Architecture.md §6).

Turns classifications into a *proposal*. `propose` is read-only on both the document tree and
the index: it reads `document` / `occurrence` / `classification` and writes exactly one file -
the plan - which the CLI forces to live outside ``[paths].root``. Nothing here renames, moves,
or opens a file under the root for writing, and nothing here writes
``document.doc_date/party/doc_type/detail``; those are committed by `apply` (phase 6) from an
approved plan.

Target paths are the security-critical seam, because phase 6 will execute them. ``party``,
``detail``, and ``doc_date`` originate in OCR text - attacker-influenceable content inside a
scanned document - and ``folder`` and ``template`` come from user config, so three independent
layers guard the result: :func:`taxonomy._check_folder` rejects an absolute or traversing
folder at load time, :func:`sanitize_component` strips separators and traversal out of every
rendered field, and :func:`plan_document` resolves the final target and refuses anything that
is not inside the root, emitting ``status='error'`` rather than a move. Template rendering
tokenizes rather than calling ``str.format``: an OCR-derived value is substituted *in*, never
interpreted *as* a template.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import db
from .taxonomy import (
    PROVENANCE_AGENT,
    PROVENANCE_RULE,
    Taxonomy,
    TaxonomyError,
    extract_date_for_rule,
    match_document,
)

PLAN_VERSION = 2
DEFAULT_TEMPLATE = "{doc_date}_{party}_{doc_type}_{detail}"

# Per-component cap; keeps total path length sane on the Windows host.
MAX_COMPONENT_CHARS = 80

TEMPLATE_FIELDS = ("doc_date", "party", "doc_type", "detail")

STATUS_MOVE = "move"
STATUS_NOOP = "noop"
STATUS_UNCLASSIFIED = "unclassified"
STATUS_COLLISION = "collision"
STATUS_ERROR = "error"

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

# The occurrence join carries three columns off one row - path plus the (mtime, size) pair
# `apply` (phase 6) compares against before it touches the file - so the stability baseline is
# recorded *into the plan* rather than re-read from the index at apply time: an `ingest` run
# between `propose` and `apply` refreshes the occurrence row, so an index-side comparison would
# silently pass for a file that did change. MIN(occurrence_id) picks the same first live
# occurrence the scalar subquery used to.
_PLAN_SQL = """
    SELECT d.document_id AS document_id,
           d.sha256      AS sha256,
           d.ocr_text    AS ocr_text,
           o.rel_path    AS rel_path,
           o.mtime       AS occ_mtime,
           o.size_bytes  AS occ_size,
           c.party       AS agent_party,
           c.doc_type    AS agent_doc_type,
           c.detail      AS agent_detail,
           c.doc_date    AS agent_doc_date,
           c.folder      AS agent_folder,
           c.tags        AS agent_tags,
           c.provenance  AS agent_provenance
    FROM document d
    LEFT JOIN classification c ON c.document_id = d.document_id
    LEFT JOIN occurrence o ON o.occurrence_id = (
        SELECT MIN(o2.occurrence_id) FROM occurrence o2
         WHERE o2.document_id = d.document_id AND o2.missing_since IS NULL)
    WHERE EXISTS (SELECT 1 FROM occurrence o
                   WHERE o.document_id = d.document_id AND o.missing_since IS NULL)
"""


@dataclass(frozen=True)
class PlanEntry:
    document_id: int
    sha256: str
    current_path: str | None = None
    target_path: str | None = None
    folder: str | None = None
    target_name: str | None = None
    fields: dict[str, str | None] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    provenance: str | None = None
    rule_id: str | None = None
    # Which selector produced `fields["doc_date"]`: rule-regex | first | last | agent, or None
    # when no date was found. Makes a wrong date diagnosable from the plan alone.
    date_source: str | None = None
    status: str = STATUS_UNCLASSIFIED
    note: str | None = None
    # The stability baseline `apply` re-checks before it moves the file (docs/Architecture.md §6):
    # the same (mtime, size) pair `ingest.upsert_file` records on the occurrence row.
    current_mtime: float | None = None
    current_size: int | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data


@dataclass(frozen=True)
class PlanSummary:
    documents: int = 0
    move: int = 0
    noop: int = 0
    unclassified: int = 0
    collision: int = 0
    errors: int = 0
    rule_matched: int = 0
    agent_matched: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _optional(row: Mapping, key: str):
    """``row[key]`` when the mapping has it, else ``None``.

    Tolerates a row that lacks the key entirely: ``sqlite3.Row`` raises ``IndexError`` and a
    hand-built dict row (what :func:`plan_document`'s unit tests pass) raises ``KeyError``.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def new_plan_id() -> str:
    """``plan-<UTC compact timestamp>-<6 hex>``; also what ``move_log.plan_id`` carries in #6."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"plan-{stamp}-{secrets.token_hex(3)}"


def sanitize_component(value: str | None) -> str:
    """Reduce one rendered field to a safe single path component.

    Everything outside ``[A-Za-z0-9._-]`` - which includes every path separator, every Windows
    illegal character, and every control character - becomes ``_``. Windows reserved device
    names are prefixed, and the result is capped, so no field of a scanned document can steer
    the target out of its folder or produce a name the host refuses.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = _UNSAFE_RE.sub("_", text)
    text = re.sub(r"_{2,}", "_", text).strip("._- ")
    text = text[:MAX_COMPONENT_CHARS].strip("._- ")
    if not text:
        return ""
    # Prefixed last: the strip above would otherwise eat the guard character straight back off.
    if text.split(".")[0].casefold() in _RESERVED:
        text = "_" + text
    return text


def render_name(template: str, fields: Mapping[str, str | None]) -> str:
    """Render ``template``, dropping an empty field together with its preceding separator run.

    Tokenized, never ``str.format``: an unknown placeholder is a config error rather than a
    ``KeyError`` traceback, and a document whose OCR text contains ``{party}`` cannot influence
    rendering because values are only ever substituted in.
    """
    if not isinstance(template, str) or not template.strip():
        raise TaxonomyError("[naming].template must be a non-empty string")

    out: list[str] = []
    pending = ""  # the literal run since the last emitted field: dropped with an empty field
    position = 0
    emitted = False
    for match in _PLACEHOLDER_RE.finditer(template):
        pending += template[position : match.start()]
        position = match.end()
        name = match[1]
        if name not in fields:
            raise TaxonomyError(
                f"[naming].template: unknown field {{{name}}} "
                f"(known: {', '.join(sorted(fields))})"
            )
        value = sanitize_component(fields.get(name))
        if not value:
            pending = ""
            continue
        if emitted:
            out.append(pending)
        out.append(value)
        pending = ""
        emitted = True
    pending += template[position:]
    if emitted and pending:
        out.append(pending)

    name = "".join(out).strip()
    if not name:
        return ""
    if any(sep in name for sep in ("/", "\\")) or name in (".", ".."):
        raise TaxonomyError(f"[naming].template renders a path, not a filename: {name!r}")
    return name


def _tags_from_json(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded if str(item).strip())


def _stored_verdict(row: Mapping) -> dict | None:
    """The agent verdict on this document, if one was submitted. Outranks any rule match."""
    if not row["agent_provenance"]:
        return None
    return {
        "party": row["agent_party"],
        "doc_type": row["agent_doc_type"],
        "detail": row["agent_detail"],
        "doc_date": row["agent_doc_date"],
        "folder": row["agent_folder"],
        "tags": _tags_from_json(row["agent_tags"]),
        "provenance": PROVENANCE_AGENT,
        "rule_id": None,
        "rule": None,  # an agent verdict has no rule, so no per-rule date keys
    }


def _rule_verdict(taxonomy: Taxonomy, text: str | None) -> dict | None:
    hit = match_document(taxonomy, text)
    if hit is None:
        return None
    return {
        "party": hit.party,
        "doc_type": hit.doc_type,
        "detail": hit.detail,
        "doc_date": None,  # rules classify; the date comes from the text
        "folder": hit.folder,
        "tags": hit.tags,
        "provenance": PROVENANCE_RULE,
        "rule_id": hit.rule_id,
        "rule": hit._rule,  # carried for its date_regex / date keys, not re-looked-up by id
    }


def plan_document(
    row: Mapping,
    *,
    taxonomy: Taxonomy,
    template: str,
    root: Path,
    taken: dict[str, int] | None = None,
) -> PlanEntry:
    """Plan one document. Pure apart from the on-disk collision probe; never writes anything."""
    document_id = int(row["document_id"])
    current_path = row["rel_path"]
    base = PlanEntry(
        document_id=document_id,
        sha256=row["sha256"],
        current_path=current_path,
        fields={name: None for name in TEMPLATE_FIELDS},
        current_mtime=_optional(row, "occ_mtime"),
        current_size=_optional(row, "occ_size"),
    )

    verdict = _stored_verdict(row) or _rule_verdict(taxonomy, row["ocr_text"])
    if verdict is None:
        return base

    # Precedence: the agent verdict's explicit date, then the matched rule's own selection.
    if verdict["doc_date"]:
        doc_date, date_source = verdict["doc_date"], PROVENANCE_AGENT
    else:
        doc_date, date_source = extract_date_for_rule(
            verdict["rule"], row["ocr_text"], date_order=taxonomy.date_order
        )
    fields = {
        "doc_date": doc_date,
        "party": verdict["party"],
        "doc_type": verdict["doc_type"],
        "detail": verdict["detail"],
    }
    common = {
        "fields": fields,
        "tags": tuple(verdict["tags"]),
        "provenance": verdict["provenance"],
        "rule_id": verdict["rule_id"],
        "date_source": date_source,
    }

    def failed(note: str) -> PlanEntry:
        return PlanEntry(**{**asdict(base), **common, "status": STATUS_ERROR, "note": note})

    try:
        name = render_name(template, fields)
    except TaxonomyError as exc:
        return failed(str(exc))
    if not name:
        return failed("every naming field is empty after sanitizing")
    if current_path is None:
        return failed("no live occurrence on disk")

    current = PurePosixPath(current_path)
    folder = verdict["folder"] or current.parent.as_posix()
    folder = "" if folder in (".", "") else folder
    target_path = f"{folder}/{name}{current.suffix}" if folder else f"{name}{current.suffix}"

    resolved_root = root.resolve()
    try:
        resolved = (resolved_root / target_path).resolve()
        inside = resolved == resolved_root or resolved.is_relative_to(resolved_root)
    except (OSError, ValueError):
        inside = False
    if not inside:
        return failed(f"target {target_path} resolves outside the document root")

    entry = PlanEntry(
        **{
            **asdict(base),
            **common,
            "target_path": target_path,
            "target_name": f"{name}{current.suffix}",
            "folder": folder or None,
            "status": STATUS_MOVE,
        }
    )
    if target_path == current_path:
        return PlanEntry(**{**asdict(entry), "status": STATUS_NOOP})

    key = target_path.casefold()  # the Windows host is case-insensitive; be strict everywhere
    if taken is not None and key in taken:
        return PlanEntry(
            **{
                **asdict(entry),
                "status": STATUS_COLLISION,
                "note": f"document {taken[key]} already claims {target_path}",
            }
        )
    if (resolved_root / target_path).exists():
        return PlanEntry(
            **{
                **asdict(entry),
                "status": STATUS_COLLISION,
                "note": f"{target_path} already exists on disk",
            }
        )
    if taken is not None:
        taken[key] = document_id
    return entry


def build_plan(
    conn: sqlite3.Connection,
    root: Path,
    *,
    taxonomy: Taxonomy,
    template: str = DEFAULT_TEMPLATE,
    limit: int | None = None,
    document_id: int | None = None,
) -> tuple[list[PlanEntry], PlanSummary]:
    """Plan every document holding a live occurrence. Read-only on the index and the tree."""
    db.require_migrated(conn)
    sql = _PLAN_SQL
    params: list = []
    if document_id is not None:
        sql += " AND d.document_id = ?"
        params.append(int(document_id))
    sql += " ORDER BY d.document_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))

    taken: dict[str, int] = {}
    entries = [
        plan_document(row, taxonomy=taxonomy, template=template, root=root, taken=taken)
        for row in conn.execute(sql, params)
    ]

    counts = {status: 0 for status in
              (STATUS_MOVE, STATUS_NOOP, STATUS_UNCLASSIFIED, STATUS_COLLISION, STATUS_ERROR)}
    rule_matched = agent_matched = 0
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
        if entry.provenance == PROVENANCE_RULE:
            rule_matched += 1
        elif entry.provenance == PROVENANCE_AGENT:
            agent_matched += 1
    summary = PlanSummary(
        documents=len(entries),
        move=counts[STATUS_MOVE],
        noop=counts[STATUS_NOOP],
        unclassified=counts[STATUS_UNCLASSIFIED],
        collision=counts[STATUS_COLLISION],
        errors=counts[STATUS_ERROR],
        rule_matched=rule_matched,
        agent_matched=agent_matched,
    )
    return entries, summary


def write_plan(
    entries: Sequence[PlanEntry],
    summary: PlanSummary,
    path: str | Path,
    *,
    plan_id: str,
    root: Path,
    taxonomy_path: str | Path,
    template: str,
) -> Path:
    """Write the plan file atomically. The contract phase 6's `apply` / `undo` reads back."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "plan_version": PLAN_VERSION,
        "plan_id": plan_id,
        "created_at": _now(),
        "root": str(root),
        "taxonomy": str(taxonomy_path),
        "template": template,
        "summary": summary.as_dict(),
        "entries": [entry.as_dict() for entry in entries],
    }
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def record_agent_classification(
    conn: sqlite3.Connection,
    document_id: int,
    *,
    party: str | None = None,
    doc_type: str | None = None,
    detail: str | None = None,
    doc_date: str | None = None,
    folder: str | None = None,
    tags: Sequence[str] = (),
    note: str | None = None,
    now: str | None = None,
) -> dict:
    """Record one agent verdict (provenance ``agent``), replacing any earlier one.

    The verdict is untrusted input: it is bound as SQL parameters and every field is sanitized
    again when the plan renders it. Raises ValueError on an unknown document or an empty verdict.
    """
    db.require_migrated(conn)
    row = conn.execute(
        "SELECT document_id FROM document WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no document {document_id}")

    def clean(value: str | None) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    party, doc_type, detail = clean(party), clean(doc_type), clean(detail)
    doc_date, note = clean(doc_date), clean(note)
    if doc_date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc_date):
        raise ValueError("doc_date must be ISO YYYY-MM-DD")
    folder = _check_verdict_folder(clean(folder))
    tag_list = [str(tag).strip() for tag in (tags or ()) if str(tag).strip()]
    if not any((party, doc_type, detail, doc_date, folder, tag_list)):
        raise ValueError("empty verdict - pass at least one of party/doc-type/detail/date/folder")

    stamp = now or _now()
    with conn:
        conn.execute(
            """
            INSERT INTO classification (document_id, party, doc_type, detail, doc_date, tags,
                                        folder, provenance, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
              party = excluded.party, doc_type = excluded.doc_type, detail = excluded.detail,
              doc_date = excluded.doc_date, tags = excluded.tags, folder = excluded.folder,
              provenance = excluded.provenance, note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (
                document_id, party, doc_type, detail, doc_date,
                json.dumps(tag_list) if tag_list else None,
                folder, PROVENANCE_AGENT, note, stamp, stamp,
            ),
        )
    return {
        "document_id": document_id,
        "party": party,
        "doc_type": doc_type,
        "detail": detail,
        "doc_date": doc_date,
        "folder": folder,
        "tags": tag_list,
        "provenance": PROVENANCE_AGENT,
        "note": note,
    }


def _check_verdict_folder(folder: str | None) -> str | None:
    if folder is None:
        return None
    normalized = folder.replace("\\", "/").strip()
    if not normalized:
        return None
    # Checked before stripping: rewriting `/etc` into `etc` would launder a rejected absolute
    # path into an accepted relative one.
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        raise ValueError("folder must be relative to the document root and may not contain '..'")
    return normalized.strip("/") or None


def suggested_rule(verdict: Mapping) -> str:
    """The paste-ready ``[[rules]]`` stanza for an agent verdict.

    The promotion leg of "agent verdicts are promotable into rules": the tool *prints* this and
    a human pastes it into their ``taxonomy.toml``. The tool never edits the rules file - the
    same posture as never moving a document unasked.
    """
    party = verdict.get("party")
    doc_type = verdict.get("doc_type")
    slug = sanitize_component(f"{party or 'party'}-{doc_type or 'document'}").casefold() or "rule"
    lines = ["[[rules]]", f"id = {json.dumps(slug)}"]
    if party:
        lines.append(f"# party = {json.dumps(sanitize_component(party).casefold())}"
                     "  # add the party to [parties] first")
    if doc_type:
        lines.append(f"doc_type = {json.dumps(doc_type)}")
    terms = [t for t in (party, doc_type) if t]
    lines.append(f"any = {json.dumps([t.casefold() for t in terms])}")
    if verdict.get("folder"):
        lines.append(f"folder = {json.dumps(verdict['folder'])}")
    if verdict.get("tags"):
        lines.append(f"tags = {json.dumps(list(verdict['tags']))}")
    lines.append("priority = 100")
    return "\n".join(lines)
