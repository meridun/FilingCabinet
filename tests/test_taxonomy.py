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
        (_with_rule(date="middle"), "date"),
        (_with_rule(date_regex=r"(20\d\d"), "date_regex"),
        (_with_rule(date_regex="statement period"), "capture group"),
        (_with_rule(date_regex=r"(20\d\d)-(\d\d)"), "capture group"),
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
        # 2-digit years and the compact month-name form (#25)
        ("3/8/26", "dmy", "2026-08-03"),
        ("3/8/26", "mdy", "2026-03-08"),
        ("Date Filled: 12/3/25", "dmy", "2025-03-12"),
        ("Date Filled: 12/3/25", "mdy", "2025-12-03"),
        # a second field > 12 self-resolves at 2-digit width too: date_order stays a tie-break
        ("12/22/25", "dmy", "2025-12-22"),
        ("12/22/25", "mdy", "2025-12-22"),
        ("01/22/26", "dmy", "2026-01-22"),
        ("01/22/26", "mdy", "2026-01-22"),
        ("renewed 29-sep-2025", "dmy", "2025-09-29"),
        ("renewed 29-sep-2025", "mdy", "2025-09-29"),  # month-name classes ignore date_order
        ("renewed 29-Sep-25", "dmy", "2025-09-29"),
        ("renewed 29-Sep-25", "mdy", "2025-09-29"),
        ("dated 31/02/26", "dmy", None),  # calendar-validated at 2-digit width as well
        ("built v1.2.26 here", "dmy", None),  # word boundary: a version is not a date
        ("ref 3/8/265", "dmy", None),  # a 3-digit run is not a year
        ("renewed 29-sep", "dmy", None),  # the compact form requires a year
        ("no date here at all", "dmy", None),
        ("", "dmy", None),
    ],
)
def test_extract_date(text, order, expected):
    assert taxonomy.extract_date(text, date_order=order) == expected


# The shape of the defect: OCR turned a footer into "June 5, 1950" on a package whose first
# line reads 01/08/2026, and class-ordered precedence let the month-name date win. Synthetic
# text in the shape of the reported document; no user document enters this repo (§8).
_POSITIONAL_TEXT = "01/08/2026 tax package - member since June 5, 1950 - see enclosed"


@pytest.mark.parametrize("order, expected", [("dmy", "2026-08-01"), ("mdy", "2026-01-08")])
def test_first_date_is_positional(order, expected):
    assert taxonomy.extract_date(_POSITIONAL_TEXT, date_order=order) == expected


def test_first_and_last_are_the_ends_of_one_positional_list():
    text = _POSITIONAL_TEXT + " printed 2026-03-04"
    assert taxonomy.extract_date(text, date_order="mdy", select="first") == "2026-01-08"
    assert taxonomy.extract_date(text, date_order="mdy", select="last") == "2026-03-04"


@pytest.mark.parametrize(
    "year_text, expected",
    [
        ("26", 2026),
        ("27", 2027),  # one year ahead is allowed
        ("28", 1928),  # further ahead is a misread: lean to the past
        ("99", 1999),
        ("2025", 2025),  # a written 4-digit year is never rewritten
    ],
)
def test_two_digit_year_century_pivot(year_text, expected):
    # `current_year` is passed explicitly: the pivot must never be asserted against the clock.
    assert taxonomy._expand_two_digit_year(year_text, current_year=2026) == expected


# --- per-rule date selection (#19) -------------------------------------------------------

# One statement-shaped fixture carrying three dates: an issue date first, then both ends of
# the statement period. First-date-wins lands on the issue date; a human files by the period
# end. Synthetic - no real party or account number ever enters this repo (Architecture §8).
MIXED_DATES = (
    "Statement issued 12 March 2026. Statement period 1 February 2026 to 28 February 2026."
)


def _rule(**overrides):
    return taxonomy.Taxonomy.from_mapping(_with_rule(**overrides)).rules[0]


def test_date_regex_selects_the_captured_date_over_an_earlier_one():
    rule = _rule(date_regex=r"\bto\s+([^.]{0,30})")
    assert taxonomy.extract_date_for_rule(rule, MIXED_DATES, date_order="dmy") == (
        "2026-02-28",
        "rule-regex",
    )


def test_date_selector_last_first_and_absent():
    for rule, expected in (
        (_rule(date="last"), ("2026-02-28", "last")),
        (_rule(date="first"), ("2026-03-12", "first")),
        (_rule(), ("2026-03-12", "first")),  # neither key: today's behaviour, unchanged
        (None, ("2026-03-12", "first")),  # and no rule at all is the same
    ):
        assert taxonomy.extract_date_for_rule(rule, MIXED_DATES, date_order="dmy") == expected
    assert taxonomy.extract_date(MIXED_DATES) == "2026-03-12"
    assert taxonomy.extract_date(MIXED_DATES, select="last") == "2026-02-28"


@pytest.mark.parametrize(
    "date_regex",
    [
        r"period ending ([^.]{0,20})",  # matches nothing
        r"Statement (issued)",  # matches, but the capture is not a date
    ],
)
@pytest.mark.parametrize(
    "select, expected", [("first", "2026-03-12"), ("last", "2026-02-28")]
)
def test_an_unusable_date_regex_falls_back_to_the_selector(date_regex, select, expected):
    rule = _rule(date_regex=date_regex, date=select)
    assert taxonomy.extract_date_for_rule(rule, MIXED_DATES, date_order="dmy") == (
        expected,
        select,  # the source says `first`/`last`, never `rule-regex`, when the regex misses
    )


def test_a_date_regex_capture_with_a_two_digit_year_parses():
    """A per-rule `date_regex` (#19) parses its capture with the same parser, so widening the
    parser is what makes a 2-digit year reachable through a rule at all."""
    rule = _rule(date_regex=r"Date Filled:\s*(\S{6,10})")
    text = "pharmacy receipt - rx 1234567 - Date Filled: 12/3/25 - qty 30"
    assert taxonomy.extract_date_for_rule(rule, text, date_order="dmy") == (
        "2025-03-12",
        "rule-regex",
    )


def test_no_parseable_date_anywhere_has_no_source():
    rule = _rule(date_regex=r"\bto\s+([^.]{0,30})", date="last")
    assert taxonomy.extract_date_for_rule(rule, "nothing dated here", date_order="dmy") == (
        None,
        None,
    )
    assert taxonomy.extract_date_for_rule(rule, None, date_order="dmy") == (None, None)


def test_match_document_carries_the_matched_rule_without_changing_its_shape():
    tax = taxonomy.Taxonomy.from_mapping(_with_rule(date="last"))
    hit = taxonomy.match_document(tax, "northwind invoice")
    assert hit._rule is tax.rules[0] and hit._rule.date_select == "last"
    # `_rule` is excluded from equality: the public shape of a Match is unchanged.
    assert hit == taxonomy.Match(
        rule_id=hit.rule_id,
        party=hit.party,
        doc_type=hit.doc_type,
        detail=hit.detail,
        folder=hit.folder,
        tags=hit.tags,
    )


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
