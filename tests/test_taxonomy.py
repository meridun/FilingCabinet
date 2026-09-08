import pytest

from filingcabinet import taxonomy

GOOD = {
    "version": 1,
    "doc_types": ["invoice", "statement"],
    "date_order": "dmy",
    "parties": {"northwind": {"display": "Northwind Supplies", "aliases": ["northwind ltd"]}},
    "rules": [
        {
            "id": "northwind-invoice",
            "party": "northwind",
            "doc_type": "invoice",
            "all": ["northwind"],
            "any": ["invoice"],
            "none": ["statement"],
            "folder": "Suppliers/Northwind",
            "tags": ["supplier"],
            "priority": 100,
        }
    ],
}


def _with_rule(**overrides):
    rule = {**GOOD["rules"][0], **overrides}
    return {**GOOD, "rules": [rule]}


def test_from_mapping_accepts_a_good_table():
    tax = taxonomy.Taxonomy.from_mapping(GOOD)
    assert tax.version == 1 and tax.date_order == "dmy"
    assert tax.doc_types == ("invoice", "statement")
    assert tax.parties["northwind"].display == "Northwind Supplies"
    rule = tax.rules[0]
    assert rule.rule_id == "northwind-invoice" and rule.folder == "Suppliers/Northwind"
    assert "northwind ltd" in rule.any_terms  # the party's aliases widen its own rule


def test_absent_mapping_is_an_empty_taxonomy():
    tax = taxonomy.Taxonomy.from_mapping(None)
    assert tax.rules == () and tax.parties == {}


@pytest.mark.parametrize(
    "mapping, fragment",
    [
        (_with_rule(doc_type="ledger"), "doc_type"),
        (_with_rule(party="unknown-party"), "party"),
        (_with_rule(regex="invoice(["), "regex"),
        (_with_rule(folder="/etc"), "folder"),
        (_with_rule(folder="../../outside"), "folder"),
        (_with_rule(folder="C:/Windows"), "folder"),
        ({**GOOD, "date_order": "ymd"}, "date_order"),
        ({**GOOD, "version": "one"}, "version"),
    ],
)
def test_from_mapping_rejects_and_names_the_offending_key(mapping, fragment):
    with pytest.raises(taxonomy.TaxonomyError) as exc:
        taxonomy.Taxonomy.from_mapping(mapping)
    assert fragment in str(exc.value)


def test_duplicate_rule_id_is_rejected():
    mapping = {**GOOD, "rules": [GOOD["rules"][0], dict(GOOD["rules"][0])]}
    with pytest.raises(taxonomy.TaxonomyError, match="duplicate rule id"):
        taxonomy.Taxonomy.from_mapping(mapping)


def test_match_document_honours_all_any_none_and_regex():
    tax = taxonomy.Taxonomy.from_mapping(
        _with_rule(all=["northwind"], any=["invoice"], none=["draft"], regex=r"invoice\s+no")
    )
    hit = taxonomy.match_document(tax, "NORTHWIND tax invoice no 12")
    assert hit is not None
    assert (hit.rule_id, hit.party, hit.doc_type) == (
        "northwind-invoice",
        "Northwind Supplies",
        "invoice",
    )
    assert hit.provenance == taxonomy.PROVENANCE_RULE
    assert taxonomy.match_document(tax, "northwind invoice no 12 draft") is None  # none
    assert taxonomy.match_document(tax, "northwind invoice 12") is None  # regex
    assert taxonomy.match_document(tax, "acme invoice no 12") is None  # all


def test_match_document_matches_on_an_alias_alone():
    tax = taxonomy.Taxonomy.from_mapping(_with_rule(all=[], any=["invoice"]))
    assert taxonomy.match_document(tax, "Northwind Ltd - remittance") is not None


def test_match_document_is_deterministic_by_priority_then_id():
    mapping = {
        **GOOD,
        "rules": [
            {"id": "b-rule", "any": ["invoice"], "priority": 10},
            {"id": "a-rule", "any": ["invoice"], "priority": 10},
            {"id": "top", "any": ["invoice"], "priority": 50},
        ],
    }
    tax = taxonomy.Taxonomy.from_mapping(mapping)
    assert [r.rule_id for r in tax.rules] == ["top", "a-rule", "b-rule"]
    assert taxonomy.match_document(tax, "an invoice").rule_id == "top"


def test_a_rule_with_no_conditions_matches_nothing():
    tax = taxonomy.Taxonomy.from_mapping({**GOOD, "rules": [{"id": "catch-all"}]})
    assert taxonomy.match_document(tax, "anything at all") is None


def test_match_document_on_empty_text_is_none():
    assert taxonomy.match_document(taxonomy.Taxonomy.from_mapping(GOOD), None) is None
    assert taxonomy.match_document(taxonomy.Taxonomy.from_mapping(GOOD), "   ") is None


@pytest.mark.parametrize(
    "text, order, expected",
    [
        ("dated 2026-02-03 ref 9", "dmy", "2026-02-03"),
        ("on 3 February 2026 the", "dmy", "2026-02-03"),
        ("on 3rd of February 2026", "dmy", "2026-02-03"),
        ("on February 3, 2026 we", "dmy", "2026-02-03"),
        ("dated 03/02/2026", "dmy", "2026-02-03"),
        ("dated 03/02/2026", "mdy", "2026-03-02"),
        ("dated 25/12/2026", "dmy", "2026-12-25"),
        ("dated 25/12/2026", "mdy", "2026-12-25"),  # unambiguous: config cannot override it
        ("dated 31/02/2026", "dmy", None),  # validated through the calendar
        ("no date here at all", "dmy", None),
        ("", "dmy", None),
    ],
)
def test_extract_date(text, order, expected):
    assert taxonomy.extract_date(text, date_order=order) == expected


def test_load_taxonomy_missing_file_is_empty_not_an_error(tmp_path):
    tax = taxonomy.load_taxonomy(tmp_path / "nope.toml")
    assert tax.rules == ()


def test_load_taxonomy_reads_a_real_file(tmp_path):
    path = tmp_path / "taxonomy.toml"
    path.write_text(
        'version = 1\ndoc_types = ["invoice"]\n\n[[rules]]\nid = "r1"\n'
        'doc_type = "invoice"\nany = ["invoice"]\n',
        encoding="utf-8",
    )
    tax = taxonomy.load_taxonomy(path)
    assert taxonomy.match_document(tax, "an Invoice").rule_id == "r1"


def test_load_taxonomy_malformed_toml_raises(tmp_path):
    path = tmp_path / "taxonomy.toml"
    path.write_text("version = = 1\n", encoding="utf-8")
    with pytest.raises(taxonomy.TaxonomyError):
        taxonomy.load_taxonomy(path)


def test_shipped_example_template_is_valid():
    """The scaffolded taxonomy.toml must load: `instance init` hands it straight to operators."""
    from importlib import resources

    path = resources.files("filingcabinet") / "templates" / "taxonomy.example.toml"
    tax = taxonomy.load_taxonomy(path)
    assert {r.rule_id for r in tax.rules} >= {"northwind-invoice", "acme-mutual-policy"}


def test_matching_is_length_capped():
    tax = taxonomy.Taxonomy.from_mapping(_with_rule(all=["northwind"], any=["invoice"]))
    padded = ("x" * taxonomy.MAX_MATCH_CHARS) + " northwind invoice"
    assert taxonomy.match_document(tax, padded) is None
