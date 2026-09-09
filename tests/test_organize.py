import json

import pytest

from filingcabinet import db, organize, taxonomy

TAXONOMY = {
    "version": 1,
    "doc_types": ["invoice", "statement"],
    "parties": {"northwind": {"display": "Northwind", "aliases": ["northwind ltd"]}},
    "rules": [
        {
            "id": "northwind-invoice",
            "party": "northwind",
            "doc_type": "invoice",
            "all": ["northwind"],
            "any": ["invoice"],
            "folder": "Suppliers/Northwind",
            "tags": ["supplier"],
            "priority": 100,
        }
    ],
}


def _tax(mapping=None):
    return taxonomy.Taxonomy.from_mapping(mapping or TAXONOMY)


def _migrated():
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn


def _add_document(conn, rel_path, text, sha=None):
    sha = sha or f"sha-{rel_path}"
    cursor = conn.execute(
        "INSERT INTO document (sha256, size_bytes, ocr_text, first_seen_at, updated_at) "
        "VALUES (?, 1, ?, 'now', 'now')",
        (sha, text),
    )
    document_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO occurrence (document_id, rel_path, mtime, size_bytes, seen_at) "
        "VALUES (?, ?, 0.0, 1, 'now')",
        (document_id, rel_path),
    )
    return document_id


# --- render_name -------------------------------------------------------------------------


def test_render_name_drops_a_missing_field_with_its_separator():
    template = organize.DEFAULT_TEMPLATE
    full = {"doc_date": "2026-02-03", "party": "Northwind", "doc_type": "invoice",
            "detail": "camden"}
    assert organize.render_name(template, full) == "2026-02-03_Northwind_invoice_camden"
    assert organize.render_name(template, {**full, "detail": None}) == \
        "2026-02-03_Northwind_invoice"
    assert organize.render_name(template, {**full, "party": None}) == \
        "2026-02-03_invoice_camden"
    only_type = {**full, "doc_date": None, "party": None, "detail": None}
    assert organize.render_name(template, only_type) == "invoice"


def test_render_name_with_every_field_empty_is_empty():
    fields = dict.fromkeys(organize.TEMPLATE_FIELDS)
    assert organize.render_name(organize.DEFAULT_TEMPLATE, fields) == ""


def test_render_name_rejects_an_unknown_placeholder():
    with pytest.raises(taxonomy.TaxonomyError, match="unknown field"):
        organize.render_name("{party}_{nope}", {"party": "x"})


def test_render_name_rejects_an_empty_template():
    with pytest.raises(taxonomy.TaxonomyError):
        organize.render_name("   ", {"party": "x"})


def test_render_name_never_treats_a_value_as_a_template():
    """A document containing `{party}` must not influence rendering (no str.format)."""
    rendered = organize.render_name(
        "{doc_type}", {"doc_type": "{party}", "party": "SECRET"}
    )
    assert "SECRET" not in rendered


def test_render_name_rejects_a_template_that_renders_a_path():
    with pytest.raises(taxonomy.TaxonomyError, match="renders a path"):
        organize.render_name("{party}/{doc_type}", {"party": "a", "doc_type": "b"})


# --- sanitize_component ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("../../etc/passwd", "etc_passwd"),
        ("a/b", "a_b"),
        ("a\\b", "a_b"),
        ("CON", "_CON"),
        ("con.pdf", "_con.pdf"),
        ("lpt9", "_lpt9"),
        ('trailing. ', "trailing"),
        ("a\x00b\x1fc", "abc"),  # control characters are dropped, not padded into separators
        ('bad<>:"|?*chars', "bad_chars"),
        ("", ""),
        (None, ""),
        ("   ", ""),
    ],
)
def test_sanitize_component(value, expected):
    assert organize.sanitize_component(value) == expected


def test_sanitize_component_caps_length():
    assert len(organize.sanitize_component("x" * 300)) == organize.MAX_COMPONENT_CHARS


def test_sanitize_component_folds_non_ascii_without_leaking_separators():
    out = organize.sanitize_component("Müller & Co / Bücher")
    assert "/" not in out and "\\" not in out and out == "M_ller_Co_B_cher"


# --- build_plan --------------------------------------------------------------------------


def test_build_plan_marks_a_rule_match(tmp_path):
    conn = _migrated()
    document_id = _add_document(conn, "inbox/scan1.pdf", "Northwind tax invoice no 7")
    entries, summary = build(conn, tmp_path)
    entry = entries[0]
    assert entry.document_id == document_id
    assert entry.provenance == "rule" and entry.rule_id == "northwind-invoice"
    assert entry.status == organize.STATUS_MOVE
    assert entry.target_path == "Suppliers/Northwind/Northwind_invoice.pdf"
    assert entry.tags == ("supplier",)
    assert summary.move == 1 and summary.rule_matched == 1 and summary.unclassified == 0


def build(conn, root, **kwargs):
    kwargs.setdefault("taxonomy", _tax())
    kwargs.setdefault("template", organize.DEFAULT_TEMPLATE)
    return organize.build_plan(conn, root, **kwargs)


def test_build_plan_uses_a_date_from_the_text(tmp_path):
    conn = _migrated()
    _add_document(conn, "scan.pdf", "Northwind invoice dated 2026-02-03")
    entries, _ = build(conn, tmp_path)
    assert entries[0].fields["doc_date"] == "2026-02-03"
    assert entries[0].target_name == "2026-02-03_Northwind_invoice.pdf"
    assert entries[0].date_source == "first"  # a rule with no date keys is first-date-wins


# A statement front page as OCR'd: an issue date, then both ends of the statement period. The
# date a human files by is the period end, which is neither the first nor the only date here.
STATEMENT_TEXT = (
    "Northwind statement of account. Issued 12 March 2026. "
    "Statement period 1 February 2026 to 28 February 2026."
)
STATEMENT_TAXONOMY = {
    **TAXONOMY,
    "rules": [
        {
            "id": "northwind-statement",
            "party": "northwind",
            "doc_type": "statement",
            "all": ["northwind"],
            "any": ["statement of account"],
            "date_regex": r"\bto\s+([^.]{0,30})",
            "folder": "Suppliers/Northwind",
            "priority": 100,
        }
    ],
}


def test_a_rule_date_regex_picks_the_statement_period_end(tmp_path):
    """AC 4's in-repo substitute for re-running the pilot: the period end, not the first date."""
    conn = _migrated()
    _add_document(conn, "scan.pdf", STATEMENT_TEXT)
    entries, _ = build(conn, tmp_path, taxonomy=_tax(STATEMENT_TAXONOMY))
    entry = entries[0]
    assert entry.fields["doc_date"] == "2026-02-28" and entry.date_source == "rule-regex"
    assert entry.target_name == "2026-02-28_Northwind_statement.pdf"


def test_a_rule_date_selector_picks_the_last_date(tmp_path):
    conn = _migrated()
    _add_document(conn, "scan.pdf", STATEMENT_TEXT)
    rule = {**STATEMENT_TAXONOMY["rules"][0], "date": "last"}
    del rule["date_regex"]
    entries, _ = build(conn, tmp_path, taxonomy=_tax({**TAXONOMY, "rules": [rule]}))
    assert entries[0].fields["doc_date"] == "2026-02-28"
    assert entries[0].date_source == "last"


def test_an_agent_date_outranks_the_rules_date_regex(tmp_path):
    conn = _migrated()
    document_id = _add_document(conn, "scan.pdf", STATEMENT_TEXT)
    organize.record_agent_classification(
        conn, document_id, party="Northwind", doc_type="statement", doc_date="2026-01-31"
    )
    entries, _ = build(conn, tmp_path, taxonomy=_tax(STATEMENT_TAXONOMY))
    assert entries[0].fields["doc_date"] == "2026-01-31"
    assert entries[0].date_source == "agent"


def test_build_plan_leaves_an_unmatched_document_unclassified(tmp_path):
    conn = _migrated()
    _add_document(conn, "mystery.pdf", "nothing recognisable here")
    entries, summary = build(conn, tmp_path)
    assert entries[0].date_source is None  # unclassified: no selector ever ran
    assert entries[0].status == organize.STATUS_UNCLASSIFIED
    assert entries[0].provenance is None and entries[0].target_path is None
    assert summary.unclassified == 1 and summary.move == 0


def test_an_agent_verdict_outranks_a_competing_rule(tmp_path):
    conn = _migrated()
    document_id = _add_document(conn, "scan.pdf", "Northwind invoice no 7")
    organize.record_agent_classification(
        conn, document_id, party="Acme", doc_type="statement", folder="Banking"
    )
    entries, summary = build(conn, tmp_path)
    entry = entries[0]
    assert entry.provenance == "agent" and entry.rule_id is None
    assert entry.target_path == "Banking/Acme_statement.pdf"
    assert summary.agent_matched == 1 and summary.rule_matched == 0


def test_a_target_equal_to_the_current_path_is_a_noop(tmp_path):
    conn = _migrated()
    document_id = _add_document(conn, "Suppliers/Northwind/Northwind_invoice.pdf", "x")
    organize.record_agent_classification(
        conn, document_id, party="Northwind", doc_type="invoice", folder="Suppliers/Northwind"
    )
    entries, summary = build(conn, tmp_path)
    assert entries[0].status == organize.STATUS_NOOP and summary.noop == 1


def test_two_documents_rendering_one_name_collide(tmp_path):
    conn = _migrated()
    _add_document(conn, "a.pdf", "Northwind invoice one", sha="sha-a")
    _add_document(conn, "b.pdf", "Northwind invoice two", sha="sha-b")
    entries, summary = build(conn, tmp_path)
    assert [e.status for e in entries] == [organize.STATUS_MOVE, organize.STATUS_COLLISION]
    assert "already claims" in entries[1].note
    assert summary.collision == 1


def test_a_target_already_on_disk_collides(tmp_path):
    conn = _migrated()
    _add_document(conn, "a.pdf", "Northwind invoice one")
    existing = tmp_path / "Suppliers" / "Northwind"
    existing.mkdir(parents=True)
    (existing / "Northwind_invoice.pdf").write_bytes(b"already here")
    entries, summary = build(conn, tmp_path)
    assert entries[0].status == organize.STATUS_COLLISION and summary.collision == 1
    assert "already exists on disk" in entries[0].note


def test_a_folder_escaping_the_root_is_an_error_never_a_move(tmp_path):
    """Guard layer 3: even if a verdict folder slipped through, the resolved target must be in."""
    conn = _migrated()
    document_id = _add_document(conn, "a.pdf", "x")
    organize.record_agent_classification(conn, document_id, party="Acme", doc_type="invoice")
    conn.execute(
        "UPDATE classification SET folder = ? WHERE document_id = ?",
        ("../../outside", document_id),
    )
    entries, summary = build(conn, tmp_path)
    assert entries[0].status == organize.STATUS_ERROR and entries[0].target_path is None
    assert "outside the document root" in entries[0].note
    assert summary.errors == 1 and summary.move == 0


def test_ocr_text_cannot_steer_the_target_out_of_the_root(tmp_path):
    conn = _migrated()
    tax = _tax(
        {
            **TAXONOMY,
            "rules": [{"id": "r", "doc_type": "invoice", "any": ["invoice"], "priority": 1}],
        }
    )
    document_id = _add_document(conn, "a.pdf", "invoice")
    organize.record_agent_classification(
        conn, document_id, party="../../../etc/passwd", doc_type="invoice"
    )
    entries, _ = build(conn, tmp_path, taxonomy=tax)
    assert entries[0].status == organize.STATUS_MOVE
    assert entries[0].target_path == "etc_passwd_invoice.pdf"


def test_build_plan_skips_documents_with_no_live_occurrence(tmp_path):
    conn = _migrated()
    document_id = _add_document(conn, "gone.pdf", "Northwind invoice")
    conn.execute(
        "UPDATE occurrence SET missing_since = 'now' WHERE document_id = ?", (document_id,)
    )
    entries, summary = build(conn, tmp_path)
    assert entries == [] and summary.documents == 0


def test_build_plan_honours_limit_and_document_filters(tmp_path):
    conn = _migrated()
    first = _add_document(conn, "a.pdf", "Northwind invoice a", sha="sha-a")
    _add_document(conn, "b.pdf", "Northwind invoice b", sha="sha-b")
    entries, _ = build(conn, tmp_path, limit=1)
    assert len(entries) == 1
    entries, _ = build(conn, tmp_path, document_id=first)
    assert [e.document_id for e in entries] == [first]


def test_build_plan_requires_a_migrated_database(tmp_path):
    with pytest.raises(db.NotMigratedError):
        build(db.connect(":memory:"), tmp_path)


def test_build_plan_writes_nothing_to_the_index(tmp_path):
    conn = _migrated()
    _add_document(conn, "a.pdf", "Northwind invoice")
    build(conn, tmp_path)
    row = conn.execute("SELECT doc_date, party, doc_type, detail FROM document").fetchone()
    assert tuple(row) == (None, None, None, None)  # propose never commits; apply (#6) does
    assert conn.execute("SELECT COUNT(*) FROM classification").fetchone()[0] == 0


# --- write_plan --------------------------------------------------------------------------


def test_write_plan_round_trips(tmp_path):
    conn = _migrated()
    _add_document(conn, "a.pdf", "Northwind invoice for Bücher")
    entries, summary = build(conn, tmp_path)
    target = tmp_path / "plans" / "plan.json"
    written = organize.write_plan(
        entries, summary, target,
        plan_id="plan-test", root=tmp_path, taxonomy_path=tmp_path / "taxonomy.toml",
        template=organize.DEFAULT_TEMPLATE,
    )
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["plan_version"] == organize.PLAN_VERSION
    assert payload["plan_id"] == "plan-test" and payload["template"] == organize.DEFAULT_TEMPLATE
    assert payload["summary"]["documents"] == 1
    assert list(payload["entries"][0]) == [
        "document_id", "sha256", "current_path", "target_path", "folder", "target_name",
        "fields", "tags", "provenance", "rule_id", "date_source", "status", "note",
        "current_mtime", "current_size",
    ]
    assert not list(target.parent.glob("*.tmp"))  # atomic write leaves no scratch behind


def test_build_plan_records_the_stability_baseline_apply_checks(tmp_path):
    """Every entry carries the occurrence (mtime, size) pair, so `apply` compares against the
    file as it was when the plan was built rather than against an index `ingest` may have
    refreshed since."""
    conn = _migrated()
    _add_document(conn, "inbox/scan1.pdf", "Northwind tax invoice no 7")
    conn.execute("UPDATE occurrence SET mtime = 1234.5, size_bytes = 4096")
    entries, _ = build(conn, tmp_path)
    assert (entries[0].current_mtime, entries[0].current_size) == (1234.5, 4096)


def test_build_plan_records_the_baseline_on_an_unclassified_entry_too(tmp_path):
    conn = _migrated()
    _add_document(conn, "inbox/mystery.pdf", "nothing a rule matches")
    conn.execute("UPDATE occurrence SET mtime = 7.0, size_bytes = 11")
    entries, _ = build(conn, tmp_path)
    assert entries[0].status == organize.STATUS_UNCLASSIFIED
    assert (entries[0].current_mtime, entries[0].current_size) == (7.0, 11)


def test_plan_document_tolerates_a_row_without_the_baseline_columns(tmp_path):
    """`plan_document`'s unit callers pass hand-built dict rows; a missing key is not an error."""
    row = {"document_id": 1, "sha256": "s", "ocr_text": "Northwind invoice", "rel_path": "a.pdf",
           "agent_provenance": None}
    entry = organize.plan_document(
        row, taxonomy=_tax(), template=organize.DEFAULT_TEMPLATE, root=tmp_path
    )
    assert entry.current_mtime is None and entry.current_size is None


def test_new_plan_id_shape():
    plan_id = organize.new_plan_id()
    assert plan_id.startswith("plan-") and len(plan_id.split("-")) == 3


# --- record_agent_classification / suggested_rule ----------------------------------------


def test_record_agent_classification_upserts():
    conn = _migrated()
    document_id = _add_document(conn, "a.pdf", "x")
    organize.record_agent_classification(conn, document_id, party="First", tags=["a"])
    verdict = organize.record_agent_classification(
        conn, document_id, party="Second", doc_type="invoice", tags=["b", "c"]
    )
    rows = conn.execute("SELECT party, tags, provenance FROM classification").fetchall()
    assert len(rows) == 1
    assert rows[0]["party"] == "Second" and rows[0]["provenance"] == "agent"
    assert json.loads(rows[0]["tags"]) == ["b", "c"]
    assert verdict["tags"] == ["b", "c"]


def test_record_agent_classification_rejects_bad_input():
    conn = _migrated()
    document_id = _add_document(conn, "a.pdf", "x")
    with pytest.raises(ValueError, match="no document"):
        organize.record_agent_classification(conn, 9999, party="x")
    with pytest.raises(ValueError, match="empty verdict"):
        organize.record_agent_classification(conn, document_id)
    with pytest.raises(ValueError, match="empty verdict"):
        organize.record_agent_classification(conn, document_id, party="   ")
    with pytest.raises(ValueError, match="ISO"):
        organize.record_agent_classification(conn, document_id, doc_date="3 Feb 2026")
    with pytest.raises(ValueError, match="folder"):
        organize.record_agent_classification(conn, document_id, folder="../escape")
    with pytest.raises(ValueError, match="folder"):
        organize.record_agent_classification(conn, document_id, folder="/etc")


def test_suggested_rule_is_paste_ready_toml():
    import tomllib

    verdict = {"party": "Acme Mutual", "doc_type": "policy", "folder": "Insurance",
               "tags": ["insurance"]}
    stanza = organize.suggested_rule(verdict)
    assert stanza.startswith("[[rules]]")
    parsed = tomllib.loads(stanza)
    rule = parsed["rules"][0]
    assert rule["doc_type"] == "policy" and rule["folder"] == "Insurance"
    assert rule["tags"] == ["insurance"] and rule["priority"] == 100
    assert "acme" in rule["id"]


def test_suggested_rule_survives_a_hostile_verdict():
    import tomllib

    stanza = organize.suggested_rule({"party": 'x"\nid = "pwned', "doc_type": None})
    assert len(tomllib.loads(stanza)["rules"]) == 1
