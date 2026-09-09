"""Phase 6 unit tests: `apply_plan`, the move log, and `undo_plan`.

The invariant under test throughout is docs/Architecture.md section 6 - a document moves only
because an explicit, human-reviewed plan entry said so, every move is logged before it happens,
and every move is reversible. Fixtures are synthesized under ``tmp_path``; nothing lands in the
repo (section 8).
"""

import json

import pytest

from filingcabinet import apply as apply_mod
from filingcabinet import db, organize


def _migrated():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def _seed(conn, root, rel_path, body=b"alpha", sha=None):
    """One document with one live occurrence, and the file itself on disk under ``root``."""
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    stat = path.stat()
    sha = sha or f"sha-{rel_path}"
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, first_seen_at, updated_at) "
        "VALUES (?, ?, 'now', 'now')",
        (sha, stat.st_size),
    )
    document_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at) "
        "VALUES (?, ?, ?, ?, 'now')",
        (document_id, rel_path, stat.st_mtime, stat.st_size),
    )
    conn.commit()
    return document_id, path


def _entry(conn, doc_id, current, target, **overrides):
    row = conn.execute(
        "SELECT mtime, size_bytes FROM occurrence WHERE rel_path = ?", (current,)
    ).fetchone()
    entry = {
        "document_id": doc_id,
        "sha256": f"sha-{current}",
        "current_path": current,
        "target_path": target,
        "folder": None,
        "target_name": target.rsplit("/", 1)[-1],
        "fields": {"doc_date": "2026-02-03", "party": "Northwind", "doc_type": "invoice",
                   "detail": None},
        "tags": [],
        "provenance": "rule",
        "rule_id": "r",
        "status": organize.STATUS_MOVE,
        "note": None,
        "current_mtime": row["mtime"] if row else None,
        "current_size": row["size_bytes"] if row else None,
    }
    entry.update(overrides)
    return entry


def _plan(entries, root, plan_id="plan-test"):
    return {
        "plan_version": organize.PLAN_VERSION,
        "plan_id": plan_id,
        "created_at": "now",
        "root": str(root),
        "taxonomy": "taxonomy.toml",
        "template": organize.DEFAULT_TEMPLATE,
        "summary": {},
        "entries": list(entries),
    }


def _log(conn, plan_id="plan-test"):
    return conn.execute(
        "SELECT * FROM move_log WHERE plan_id = ? ORDER BY move_id", (plan_id,)
    ).fetchall()


def _tree(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# --- read_plan ---------------------------------------------------------------------------


def test_read_plan_round_trips_what_write_plan_emits(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan([], tmp_path)), encoding="utf-8")
    assert apply_mod.read_plan(path)["plan_id"] == "plan-test"


@pytest.mark.parametrize(
    "payload, match",
    [
        ("not json at all", "not valid JSON"),
        ('["a list"]', "not a JSON object"),
        ('{"plan_version": 1, "plan_id": "p", "entries": []}', "plan_version"),
        ('{"plan_version": 2, "entries": []}', "no plan_id"),
        ('{"plan_version": 2, "plan_id": "p"}', "no entries list"),
    ],
)
def test_read_plan_refuses_an_unusable_plan(tmp_path, payload, match):
    path = tmp_path / "plan.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(apply_mod.PlanError, match=match):
        apply_mod.read_plan(path)


def test_read_plan_refuses_a_missing_file(tmp_path):
    with pytest.raises(apply_mod.PlanError, match="cannot read plan"):
        apply_mod.read_plan(tmp_path / "absent.json")


# --- apply_plan: the happy path ------------------------------------------------------------


def test_apply_moves_logs_and_reindexes(tmp_path):
    conn = _migrated()
    document_id, source = _seed(conn, tmp_path, "inbox/scan.pdf")
    plan = _plan([_entry(conn, document_id, "inbox/scan.pdf", "Suppliers/inv.pdf")], tmp_path)

    outcomes, summary = apply_mod.apply_plan(conn, plan, root=tmp_path)

    assert (summary.moved, summary.skipped, summary.ignored, summary.errors) == (1, 0, 0, 0)
    assert outcomes[0].status == apply_mod.STATUS_MOVED and outcomes[0].reason is None
    assert not source.exists() and (tmp_path / "Suppliers/inv.pdf").read_bytes() == b"alpha"

    rows = _log(conn)
    assert len(rows) == 1
    assert (rows[0]["from_path"], rows[0]["to_path"]) == ("inbox/scan.pdf", "Suppliers/inv.pdf")
    assert rows[0]["undone_at"] is None and rows[0]["applied_at"]

    occurrence = conn.execute("SELECT * FROM occurrence").fetchone()
    landed = (tmp_path / "Suppliers/inv.pdf").stat()
    assert occurrence["rel_path"] == "Suppliers/inv.pdf"
    assert (occurrence["mtime"], occurrence["size_bytes"]) == (landed.st_mtime, landed.st_size)


def test_apply_commits_the_document_fields_from_the_plan(tmp_path):
    """`apply` is the one place doc_date/party/doc_type/detail are written.

    organize.py's module docstring promises `propose` never writes them.
    """
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf")
    entry["fields"] = {"doc_date": "2026-02-03", "party": "Northwind", "doc_type": "invoice",
                       "detail": "camden"}
    apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    row = conn.execute("SELECT * FROM document WHERE document_id = ?", (document_id,)).fetchone()
    assert (row["doc_date"], row["party"]) == ("2026-02-03", "Northwind")
    assert (row["doc_type"], row["detail"]) == ("invoice", "camden")


def test_apply_drops_a_doc_date_that_is_not_an_iso_date(tmp_path):
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf")
    entry["fields"] = {**entry["fields"], "doc_date": "3 February 2026"}
    apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    row = conn.execute("SELECT doc_date FROM document").fetchone()
    assert row["doc_date"] is None


# --- apply_plan: skips never abort the batch ------------------------------------------------


def test_a_changed_file_is_skipped_unlogged_and_the_rest_still_applies(tmp_path):
    conn = _migrated()
    stale_id, stale = _seed(conn, tmp_path, "one.pdf", b"alpha")
    good_id, _ = _seed(conn, tmp_path, "two.pdf", b"beta", sha="sha-two")
    entries = [
        _entry(conn, stale_id, "one.pdf", "moved-one.pdf"),
        _entry(conn, good_id, "two.pdf", "moved-two.pdf"),
    ]
    stale.write_bytes(b"alpha and then some")  # changed since propose

    outcomes, summary = apply_mod.apply_plan(conn, _plan(entries, tmp_path), root=tmp_path)

    assert (summary.moved, summary.skipped) == (1, 1)
    skipped = outcomes[0]
    assert skipped.status == apply_mod.STATUS_SKIPPED and "size changed" in skipped.reason
    assert stale.exists() and not (tmp_path / "moved-one.pdf").exists()
    assert [row["from_path"] for row in _log(conn)] == ["two.pdf"]  # nothing logged for the skip


def test_a_touched_file_is_skipped_on_mtime_alone(tmp_path):
    conn = _migrated()
    document_id, source = _seed(conn, tmp_path, "a.pdf", b"alpha")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf")
    entry["current_mtime"] = entry["current_mtime"] + 500.0

    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    assert summary.skipped == 1 and "mtime changed" in outcomes[0].reason
    assert source.exists() and _log(conn) == []


def test_a_locked_file_is_skipped_not_fatal(tmp_path, monkeypatch):
    conn = _migrated()
    document_id, source = _seed(conn, tmp_path, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf")

    real_open = type(source).open

    def refuse(self, *args, **kwargs):
        if self.name == "a.pdf":
            raise OSError(13, "held by another process")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(type(source), "open", refuse)
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    assert summary.skipped == 1 and "locked or unreadable" in outcomes[0].reason
    assert source.exists() and _log(conn) == []


def test_a_missing_file_is_skipped(tmp_path):
    conn = _migrated()
    document_id, source = _seed(conn, tmp_path, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf")
    source.unlink()
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    assert summary.skipped == 1 and "cannot stat" in outcomes[0].reason


def test_an_existing_target_is_skipped_never_clobbered(tmp_path):
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "a.pdf", b"alpha")
    (tmp_path / "b.pdf").write_bytes(b"someone else")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf")
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    assert summary.skipped == 1 and outcomes[0].reason == "target already exists"
    assert (tmp_path / "b.pdf").read_bytes() == b"someone else"


def test_a_case_only_rename_is_not_a_collision(tmp_path):
    """`foo.pdf` -> `Foo.pdf` must not read as an existing target on the Windows host."""
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "foo.pdf")
    entry = _entry(conn, document_id, "foo.pdf", "Foo.pdf")
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    assert summary.moved == 1 and outcomes[0].status == apply_mod.STATUS_MOVED
    assert _tree(tmp_path) == ["Foo.pdf"]


def test_a_failing_rename_leaves_no_orphan_log_row(tmp_path, monkeypatch):
    conn = _migrated()
    document_id, source = _seed(conn, tmp_path, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf")
    monkeypatch.setattr(
        apply_mod.os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "device error"))
    )
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    assert (summary.moved, summary.skipped, summary.errors) == (0, 1, 1)
    assert "move failed" in outcomes[0].reason
    assert _log(conn) == [] and source.exists()


def test_non_move_statuses_are_ignored_and_never_retried(tmp_path):
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "a.pdf")
    entries = [
        _entry(conn, document_id, "a.pdf", "b.pdf", status=status)
        for status in (organize.STATUS_NOOP, organize.STATUS_UNCLASSIFIED,
                       organize.STATUS_COLLISION, organize.STATUS_ERROR)
    ]
    outcomes, summary = apply_mod.apply_plan(conn, _plan(entries, tmp_path), root=tmp_path)
    assert (summary.ignored, summary.moved, summary.skipped) == (4, 0, 0)
    assert all(o.status == apply_mod.STATUS_IGNORED for o in outcomes)
    assert _tree(tmp_path) == ["a.pdf"] and _log(conn) == []


def test_a_malformed_entry_is_ignored(tmp_path):
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "a.pdf")
    entries = [
        "not an object",
        _entry(conn, document_id, "a.pdf", "b.pdf", document_id=None),
    ]
    outcomes, summary = apply_mod.apply_plan(conn, _plan(entries, tmp_path), root=tmp_path)
    assert summary.ignored == 2 and all(o.status == apply_mod.STATUS_IGNORED for o in outcomes)
    assert _tree(tmp_path) == ["a.pdf"]


# --- apply_plan: the plan file is untrusted input --------------------------------------------


@pytest.mark.parametrize("target", ["../evil.pdf", "sub/../../evil.pdf"])
def test_a_tampered_target_path_is_refused(tmp_path, target):
    root = tmp_path / "root"
    root.mkdir()
    conn = _migrated()
    document_id, source = _seed(conn, root, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", target)
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], root), root=root)
    assert summary.skipped == 1
    assert outcomes[0].reason == "target_path is outside the document root"
    assert source.exists() and not (tmp_path / "evil.pdf").exists()
    assert _log(conn) == []


def test_an_absolute_target_path_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    conn = _migrated()
    document_id, source = _seed(conn, root, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", str(tmp_path / "escaped.pdf"))
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], root), root=root)
    assert summary.skipped == 1 and "outside the document root" in outcomes[0].reason
    assert source.exists() and not (tmp_path / "escaped.pdf").exists()


def test_a_tampered_current_path_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.pdf").write_bytes(b"not ours")
    conn = _migrated()
    document_id, _ = _seed(conn, root, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", "b.pdf", current_path="../outside.pdf")
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], root), root=root)
    assert summary.skipped == 1
    assert outcomes[0].reason == "current_path is outside the document root"
    assert (tmp_path / "outside.pdf").exists()


def test_a_hostile_fields_value_never_reaches_the_filesystem(tmp_path):
    """`fields` is display-only: the name is never re-rendered from it (Architecture section 6)."""
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "a.pdf")
    entry = _entry(conn, document_id, "a.pdf", "Suppliers/clean.pdf")
    entry["fields"] = {**entry["fields"], "party": "../../../etc/passwd", "detail": "x/../y"}
    outcomes, summary = apply_mod.apply_plan(conn, _plan([entry], tmp_path), root=tmp_path)
    assert summary.moved == 1 and outcomes[0].status == apply_mod.STATUS_MOVED
    assert _tree(tmp_path) == ["Suppliers/clean.pdf"]
    # The raw value is still stored verbatim - it is data, bound as a parameter, never a path.
    assert conn.execute("SELECT party FROM document").fetchone()["party"] == "../../../etc/passwd"


# --- apply_plan: --dry-run --------------------------------------------------------------------


def test_apply_dry_run_writes_nothing_at_all(tmp_path):
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, "inbox/a.pdf")
    entry = _entry(conn, document_id, "inbox/a.pdf", "Suppliers/b.pdf")
    outcomes, summary = apply_mod.apply_plan(
        conn, _plan([entry], tmp_path), root=tmp_path, dry_run=True
    )
    assert summary.dry_run is True and summary.moved == 1
    assert outcomes[0].status == apply_mod.STATUS_MOVED
    assert _tree(tmp_path) == ["inbox/a.pdf"]
    assert _log(conn) == []
    row = conn.execute("SELECT * FROM document").fetchone()
    assert row["party"] is None and row["doc_date"] is None
    assert conn.execute("SELECT rel_path FROM occurrence").fetchone()["rel_path"] == "inbox/a.pdf"


def test_apply_requires_a_migrated_database(tmp_path):
    conn = db.connect(":memory:")
    with pytest.raises(db.NotMigratedError):
        apply_mod.apply_plan(conn, _plan([], tmp_path), root=tmp_path)


# --- undo_plan ---------------------------------------------------------------------------


def _applied(tmp_path, rel_path="inbox/a.pdf", target="Suppliers/b.pdf"):
    conn = _migrated()
    document_id, _ = _seed(conn, tmp_path, rel_path)
    plan = _plan([_entry(conn, document_id, rel_path, target)], tmp_path)
    apply_mod.apply_plan(conn, plan, root=tmp_path)
    return conn, document_id


def test_undo_reverses_the_move_and_stamps_the_log(tmp_path):
    conn, _ = _applied(tmp_path)
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)

    assert (summary.reversed, summary.skipped, summary.errors) == (1, 0, 0)
    assert outcomes[0].status == apply_mod.STATUS_REVERSED
    assert (outcomes[0].from_path, outcomes[0].to_path) == ("Suppliers/b.pdf", "inbox/a.pdf")
    assert _tree(tmp_path) == ["inbox/a.pdf"]

    row = _log(conn)[0]
    assert row["undone_at"] is not None
    restored = (tmp_path / "inbox/a.pdf").stat()
    occurrence = conn.execute("SELECT * FROM occurrence").fetchone()
    assert occurrence["rel_path"] == "inbox/a.pdf"
    assert (occurrence["mtime"], occurrence["size_bytes"]) == (restored.st_mtime, restored.st_size)


def test_a_second_undo_reports_nothing_left_to_reverse(tmp_path):
    conn, _ = _applied(tmp_path)
    apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    assert outcomes == [] and summary.entries == 0 and summary.reversed == 0
    assert _tree(tmp_path) == ["inbox/a.pdf"]


def test_undo_of_an_unknown_plan_id_is_a_report_not_an_error(tmp_path):
    conn = _migrated()
    outcomes, summary = apply_mod.undo_plan(conn, "plan-nope", root=tmp_path)
    assert outcomes == [] and summary.plan_id == "plan-nope" and summary.entries == 0


def test_undo_skips_a_row_whose_target_vanished(tmp_path):
    conn, _ = _applied(tmp_path)
    (tmp_path / "Suppliers/b.pdf").unlink()
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    assert summary.skipped == 1 and outcomes[0].reason == "target no longer on disk"
    assert _log(conn)[0]["undone_at"] is None  # still reversible if the file comes back


def test_undo_skips_a_file_that_changed_since_apply(tmp_path):
    conn, _ = _applied(tmp_path)
    (tmp_path / "Suppliers/b.pdf").write_bytes(b"edited since the move")
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    assert summary.skipped == 1 and "size changed" in outcomes[0].reason
    assert _tree(tmp_path) == ["Suppliers/b.pdf"]


def test_undo_skips_an_occupied_original_path(tmp_path):
    conn, _ = _applied(tmp_path)
    (tmp_path / "inbox/a.pdf").write_bytes(b"something new landed here")
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    assert summary.skipped == 1 and outcomes[0].reason == "the original path is occupied"
    assert (tmp_path / "inbox/a.pdf").read_bytes() == b"something new landed here"


def test_undo_without_an_index_baseline_still_reverses_and_says_so(tmp_path):
    conn, _ = _applied(tmp_path)
    conn.execute("DELETE FROM occurrence")
    conn.commit()
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    assert summary.reversed == 1 and outcomes[0].reason == "no indexed baseline; lock probe only"
    assert _tree(tmp_path) == ["inbox/a.pdf"]


def test_undo_dry_run_writes_nothing(tmp_path):
    conn, _ = _applied(tmp_path)
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path, dry_run=True)
    assert summary.dry_run is True and summary.reversed == 1
    assert outcomes[0].status == apply_mod.STATUS_REVERSED
    assert _tree(tmp_path) == ["Suppliers/b.pdf"]
    assert _log(conn)[0]["undone_at"] is None
    assert conn.execute("SELECT rel_path FROM occurrence").fetchone()["rel_path"] == \
        "Suppliers/b.pdf"


def test_undo_skips_a_logged_path_outside_the_root(tmp_path):
    """A hand-edited move_log row cannot make `undo` write outside the tree."""
    root = tmp_path / "root"
    root.mkdir()
    conn, _ = _applied(root)
    conn.execute("UPDATE move_log SET from_path = '../escaped.pdf'")
    conn.commit()
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=root)
    assert summary.skipped == 1
    assert outcomes[0].reason == "logged path is outside the document root"
    assert not (tmp_path / "escaped.pdf").exists()


def test_a_failing_reverse_leaves_the_row_reversible(tmp_path, monkeypatch):
    conn, _ = _applied(tmp_path)
    monkeypatch.setattr(
        apply_mod.os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "device error"))
    )
    outcomes, summary = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    assert (summary.reversed, summary.skipped, summary.errors) == (0, 1, 1)
    assert "reverse failed" in outcomes[0].reason
    assert _log(conn)[0]["undone_at"] is None


def test_undo_requires_a_migrated_database(tmp_path):
    conn = db.connect(":memory:")
    with pytest.raises(db.NotMigratedError):
        apply_mod.undo_plan(conn, "plan-test", root=tmp_path)


def test_apply_then_undo_round_trips_the_tree(tmp_path):
    conn = _migrated()
    first, _ = _seed(conn, tmp_path, "inbox/one.pdf", b"alpha")
    second, _ = _seed(conn, tmp_path, "inbox/two.pdf", b"beta", sha="sha-two")
    plan = _plan(
        [
            _entry(conn, first, "inbox/one.pdf", "A/one.pdf"),
            _entry(conn, second, "inbox/two.pdf", "B/two.pdf"),
        ],
        tmp_path,
    )
    before = _tree(tmp_path)
    _, applied = apply_mod.apply_plan(conn, plan, root=tmp_path)
    assert applied.moved == 2 and _tree(tmp_path) == ["A/one.pdf", "B/two.pdf"]
    _, reversed_ = apply_mod.undo_plan(conn, "plan-test", root=tmp_path)
    assert reversed_.reversed == 2 and _tree(tmp_path) == before
    assert all(row["undone_at"] for row in _log(conn))
