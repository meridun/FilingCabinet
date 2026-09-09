"""Phase 5 taxonomy: the instance-side rules file, rule matching, and date extraction
(docs/Architecture.md §6).

Owns the deterministic half of classification. Rules live in the operator's own
``taxonomy.toml`` (instance-side - never in this framework repo, per §8/§9); this module loads
and validates that file, matches a document's OCR text against it, and pulls a document date
out of the text.

What it refuses to do: it never writes the taxonomy file (an agent verdict is promoted into
rules by a human pasting a printed stanza), never touches the document tree, and never guesses.
`match_document` returns ``None`` rather than a low-confidence match, `extract_date` returns
``None`` rather than an unparseable date, and a malformed table raises :class:`TaxonomyError`
naming the offending key instead of half-loading. An *absent* file is not an error: it yields an
empty taxonomy, so every document routes to the agent.

Config is user input and OCR text is attacker-influenceable content from inside a scanned
document: rule regexes are compiled once at load (an uncompilable one is a config error),
matching runs against a length-capped normalized slice, and a ``folder`` that is absolute or
contains ``..`` is rejected here - the first of the three layers guarding the target path.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

# Matching runs over this many characters of normalized text at most, so an operator-authored
# regex cannot become unbounded work on a long document.
MAX_MATCH_CHARS = 200_000

PROVENANCE_RULE = "rule"
PROVENANCE_AGENT = "agent"

DEFAULT_DATE_ORDER = "dmy"
_DATE_ORDERS = ("dmy", "mdy")
# Per-rule fallback selector when no `date_regex` is given, or when it finds nothing.
_DATE_SELECTORS = ("first", "last")

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH_ALTERNATION = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Ordered: the first pattern that yields a *valid* calendar date wins. ISO first because it is
# unambiguous; the numeric form last because it is the only one that can need `date_order`.
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_TEXT_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALTERNATION})\.?,?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
_MDY_TEXT_RE = re.compile(
    rf"\b({_MONTH_ALTERNATION})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
_NUMERIC_RE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")

_WHITESPACE_RE = re.compile(r"\s+")


class TaxonomyError(ValueError):
    """A malformed taxonomy file or template. Surfaced by the CLI as `error: ...`, exit 2."""


@dataclass(frozen=True)
class Party:
    key: str
    display: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rule:
    rule_id: str
    party: str | None = None
    doc_type: str | None = None
    detail: str | None = None
    folder: str | None = None
    tags: tuple[str, ...] = ()
    all_terms: tuple[str, ...] = ()
    any_terms: tuple[str, ...] = ()
    none_terms: tuple[str, ...] = ()
    regex: str | None = None
    priority: int = 0
    # Which date in the document this rule means. `date_regex` captures it outright; the
    # `date` TOML key (here `date_select`, to avoid shadowing) is the fallback selector.
    date_regex: str | None = None
    date_select: str | None = None
    _pattern: re.Pattern[str] | None = field(default=None, compare=False, repr=False)
    _date_pattern: re.Pattern[str] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Match:
    rule_id: str | None
    party: str | None = None
    doc_type: str | None = None
    detail: str | None = None
    folder: str | None = None
    tags: tuple[str, ...] = ()
    provenance: str = PROVENANCE_RULE
    # The rule that produced this match, for callers that need its date keys. Excluded from
    # equality and repr, so `Match`'s public shape is unchanged.
    _rule: "Rule | None" = field(default=None, compare=False, repr=False)


def _require_mapping(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise TaxonomyError(f"{where} must be a table")
    return value


def _string_list(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TaxonomyError(f"{where} must be a list of strings")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise TaxonomyError(f"{where} must be a list of strings")
        item = item.strip()
        if item:
            out.append(item)
    return tuple(out)


def _optional_string(value, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaxonomyError(f"{where} must be a string")
    value = value.strip()
    return value or None


def _check_folder(folder: str | None, where: str) -> str | None:
    """Reject an absolute or traversing folder at load time (guard layer 1 of 3)."""
    if folder is None:
        return None
    normalized = folder.replace("\\", "/").strip()
    if not normalized:
        return None
    # Checked before any stripping: silently rewriting `/etc` into `etc` would turn a rejected
    # absolute path into an accepted relative one.
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in PurePosixPath(normalized).parts
    ):
        raise TaxonomyError(f"{where} must be a relative folder inside the document root")
    return normalized.strip("/") or None


def normalize_text(text: str | None) -> str:
    """Casefolded, whitespace-collapsed, length-capped view of a document's text."""
    if not text:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text))
    return collapsed.casefold()[:MAX_MATCH_CHARS]


@dataclass(frozen=True)
class Taxonomy:
    version: int = 1
    doc_types: tuple[str, ...] = ()
    parties: dict[str, Party] = field(default_factory=dict)
    rules: tuple[Rule, ...] = ()
    date_order: str = DEFAULT_DATE_ORDER

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> "Taxonomy":
        """Build from a parsed ``taxonomy.toml``.

        Tolerant of an absent table (an empty taxonomy routes everything to the agent), strict
        about a malformed one - the same split :meth:`ocr.OcrConfig.from_mapping` makes between
        "absent" and "wrong", except that a wrong taxonomy raises rather than silently
        defaulting: a typo in a rule must not quietly stop classifying documents.
        """
        if mapping is None:
            return cls()
        mapping = _require_mapping(mapping, "taxonomy")

        raw_version = mapping.get("version", 1)
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise TaxonomyError("version must be an integer")

        doc_types = _string_list(mapping.get("doc_types"), "doc_types")
        known_doc_types = {name.casefold() for name in doc_types}

        date_order = (_optional_string(mapping.get("date_order"), "date_order") or
                      DEFAULT_DATE_ORDER).lower()
        if date_order not in _DATE_ORDERS:
            raise TaxonomyError(f"date_order must be one of {', '.join(_DATE_ORDERS)}")

        parties: dict[str, Party] = {}
        for key, raw in _require_mapping(mapping.get("parties", {}), "parties").items():
            raw = _require_mapping(raw, f"parties.{key}")
            parties[key] = Party(
                key=key,
                display=_optional_string(raw.get("display"), f"parties.{key}.display") or key,
                aliases=_string_list(raw.get("aliases"), f"parties.{key}.aliases"),
            )

        raw_rules = mapping.get("rules", [])
        if not isinstance(raw_rules, (list, tuple)):
            raise TaxonomyError("rules must be an array of tables")

        rules: list[Rule] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_rules):
            raw = _require_mapping(raw, f"rules[{index}]")
            rule_id = _optional_string(raw.get("id"), f"rules[{index}].id")
            if not rule_id:
                raise TaxonomyError(f"rules[{index}].id is required")
            where = f"rules.{rule_id}"
            if rule_id in seen:
                raise TaxonomyError(f"{where}: duplicate rule id")
            seen.add(rule_id)

            party = _optional_string(raw.get("party"), f"{where}.party")
            if party is not None and party not in parties:
                raise TaxonomyError(f"{where}.party: unknown party key {party!r}")
            doc_type = _optional_string(raw.get("doc_type"), f"{where}.doc_type")
            if doc_type is not None and known_doc_types and doc_type.casefold() not in (
                known_doc_types
            ):
                raise TaxonomyError(f"{where}.doc_type: {doc_type!r} is not in doc_types")

            regex = _optional_string(raw.get("regex"), f"{where}.regex")
            pattern = None
            if regex is not None:
                try:
                    pattern = re.compile(regex, re.IGNORECASE)
                except re.error as exc:
                    raise TaxonomyError(f"{where}.regex: {exc}") from exc

            date_select = _optional_string(raw.get("date"), f"{where}.date")
            if date_select is not None:
                date_select = date_select.lower()
                if date_select not in _DATE_SELECTORS:
                    raise TaxonomyError(
                        f"{where}.date must be one of {', '.join(_DATE_SELECTORS)}"
                    )

            date_regex = _optional_string(raw.get("date_regex"), f"{where}.date_regex")
            date_pattern = None
            if date_regex is not None:
                try:
                    date_pattern = re.compile(date_regex, re.IGNORECASE)
                except re.error as exc:
                    raise TaxonomyError(f"{where}.date_regex: {exc}") from exc
                # Exactly one group: the captured text is what the date parser is handed, so
                # "which group did you mean" must never be a runtime question.
                if date_pattern.groups != 1:
                    raise TaxonomyError(
                        f"{where}.date_regex must have exactly one capture group"
                    )

            priority = raw.get("priority", 0)
            if not isinstance(priority, int) or isinstance(priority, bool):
                raise TaxonomyError(f"{where}.priority must be an integer")

            any_terms = _string_list(raw.get("any"), f"{where}.any")
            if party is not None:  # a party's aliases widen its own rules, nothing else
                aliases = tuple(a for a in parties[party].aliases if a not in any_terms)
                any_terms = any_terms + aliases

            rules.append(
                Rule(
                    rule_id=rule_id,
                    party=party,
                    doc_type=doc_type,
                    detail=_optional_string(raw.get("detail"), f"{where}.detail"),
                    folder=_check_folder(
                        _optional_string(raw.get("folder"), f"{where}.folder"), f"{where}.folder"
                    ),
                    tags=_string_list(raw.get("tags"), f"{where}.tags"),
                    all_terms=_string_list(raw.get("all"), f"{where}.all"),
                    any_terms=any_terms,
                    none_terms=_string_list(raw.get("none"), f"{where}.none"),
                    regex=regex,
                    priority=priority,
                    date_regex=date_regex,
                    date_select=date_select,
                    _pattern=pattern,
                    _date_pattern=date_pattern,
                )
            )

        # Descending priority, then rule_id ascending: a total order, so a plan is reproducible.
        ordered = tuple(sorted(rules, key=lambda r: (-r.priority, r.rule_id)))
        return cls(
            version=raw_version,
            doc_types=doc_types,
            parties=parties,
            rules=ordered,
            date_order=date_order,
        )

    def party_display(self, key: str | None) -> str | None:
        if key is None:
            return None
        party = self.parties.get(key)
        return party.display if party else key


def load_taxonomy(path: str | Path) -> Taxonomy:
    """Load ``taxonomy.toml``. A missing file yields an empty taxonomy, never an error."""
    path = Path(path)
    if not path.is_file():
        return Taxonomy()
    try:
        with path.open("rb") as fh:
            mapping = tomllib.load(fh)
    except OSError as exc:
        raise TaxonomyError(f"cannot read taxonomy {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TaxonomyError(f"{path}: {exc}") from exc
    return Taxonomy.from_mapping(mapping)


def _rule_matches(rule: Rule, haystack: str) -> bool:
    if any(term.casefold() not in haystack for term in rule.all_terms):
        return False
    if rule.any_terms and not any(term.casefold() in haystack for term in rule.any_terms):
        return False
    if any(term.casefold() in haystack for term in rule.none_terms):
        return False
    if rule._pattern is not None and not rule._pattern.search(haystack):
        return False
    # A rule with no conditions at all matches nothing: a catch-all would silently claim every
    # unclassified document, which is exactly the guess this phase refuses to make.
    return bool(rule.all_terms or rule.any_terms or rule._pattern)


def match_document(taxonomy: Taxonomy, text: str | None) -> Match | None:
    """First rule that matches ``text`` in (priority desc, rule_id asc) order, else ``None``."""
    haystack = normalize_text(text)
    if not haystack:
        return None
    for rule in taxonomy.rules:
        if _rule_matches(rule, haystack):
            return Match(
                rule_id=rule.rule_id,
                party=taxonomy.party_display(rule.party),
                doc_type=rule.doc_type,
                detail=rule.detail,
                folder=rule.folder,
                tags=rule.tags,
                provenance=PROVENANCE_RULE,
                _rule=rule,
            )
    return None


def _iso(year: int, month: int, day: int) -> str | None:
    """Validate through the calendar, so 31/02/2026 is rejected rather than emitted."""
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _date_window(text: str) -> str:
    """The one normalized, length-capped slice every date scan runs over."""
    return unicodedata.normalize("NFKC", text)[:MAX_MATCH_CHARS]


def _numeric_iso(first: int, second: int, year: int, date_order: str) -> str | None:
    """Resolve a numeric date: the document decides when it can, else ``date_order``."""
    if first > 12:  # unambiguous: only a day can exceed 12
        day, month = first, second
    elif second > 12:
        day, month = second, first
    elif date_order == "mdy":
        day, month = second, first
    else:
        day, month = first, second
    return _iso(year, month, day)


def _first_date(window: str, date_order: str) -> str | None:
    """Today's first-date behaviour, verbatim: the first *pattern class* that yields a valid
    calendar date wins - ISO, then D-Month-Y, then Month-D-Y, then numeric. Deliberately not
    positional: a document that mixes formats must keep resolving the way it always has.
    """
    match = _ISO_RE.search(window)
    if match:
        iso = _iso(int(match[1]), int(match[2]), int(match[3]))
        if iso:
            return iso

    for pattern, order in ((_DMY_TEXT_RE, "dmy"), (_MDY_TEXT_RE, "mdy")):
        match = pattern.search(window)
        if match:
            day, month_name = (match[1], match[2]) if order == "dmy" else (match[2], match[1])
            iso = _iso(int(match[3]), _MONTHS[month_name.casefold()], int(day))
            if iso:
                return iso

    match = _NUMERIC_RE.search(window)
    if match:
        iso = _numeric_iso(int(match[1]), int(match[2]), int(match[3]), date_order)
        if iso:
            return iso
    return None


def _scan_dates(window: str, date_order: str) -> list[tuple[int, str]]:
    """Every parseable date in ``window`` as ``(start offset, ISO)``, positional order.

    Unlike :func:`_first_date` this is positional across all four pattern classes, because
    "the last date in the document" is a question about position. Sorted by offset and then by
    match length, so the tie-break at one offset is the longest (most specific) match.
    """
    found: list[tuple[int, int, str]] = []

    for match in _ISO_RE.finditer(window):
        iso = _iso(int(match[1]), int(match[2]), int(match[3]))
        if iso:
            found.append((match.start(), match.end() - match.start(), iso))

    for pattern, order in ((_DMY_TEXT_RE, "dmy"), (_MDY_TEXT_RE, "mdy")):
        for match in pattern.finditer(window):
            day, month_name = (match[1], match[2]) if order == "dmy" else (match[2], match[1])
            iso = _iso(int(match[3]), _MONTHS[month_name.casefold()], int(day))
            if iso:
                found.append((match.start(), match.end() - match.start(), iso))

    for match in _NUMERIC_RE.finditer(window):
        iso = _numeric_iso(int(match[1]), int(match[2]), int(match[3]), date_order)
        if iso:
            found.append((match.start(), match.end() - match.start(), iso))

    found.sort(key=lambda hit: (hit[0], hit[1]))
    return [(start, iso) for start, _length, iso in found]


def _last_date(window: str, date_order: str) -> str | None:
    dates = _scan_dates(window, date_order)
    return dates[-1][1] if dates else None


def extract_date(
    text: str | None,
    *,
    date_order: str = DEFAULT_DATE_ORDER,
    select: str = "first",
) -> str | None:
    """A date from ``text``, normalized to ISO ``YYYY-MM-DD``.

    ``select="first"`` (the default, and what every caller got before per-rule selection
    existed) takes the first parseable date; ``select="last"`` takes the last one, which is how
    a statement's period-end date is reached when no ``date_regex`` names it.

    Ambiguity is resolved by the document, then by config: a numeric date whose first field is
    greater than 12 is unambiguous and resolves itself; otherwise ``date_order`` decides.
    Returns ``None`` rather than guessing when nothing parses.
    """
    if not text:
        return None
    window = _date_window(text)
    if select == "last":
        return _last_date(window, date_order)
    return _first_date(window, date_order)


def extract_date_for_rule(
    rule: Rule | None,
    text: str | None,
    *,
    date_order: str = DEFAULT_DATE_ORDER,
) -> tuple[str | None, str | None]:
    """The date a *rule* means, plus where it came from: ``(ISO date, date_source)``.

    ``date_source`` is ``"rule-regex"``, ``"first"``, ``"last"``, or ``None`` when nothing
    parsed - recorded on the plan entry so a wrong date is diagnosable from the plan alone.
    Precedence: ``date_regex`` first, then the ``date`` selector as its fallback (a regex that
    matches nothing, or captures something unparseable, is not an error - it degrades). A rule
    with neither key, and no rule at all, are both today's first-date behaviour exactly.
    """
    if not text:
        return (None, None)
    window = _date_window(text)

    if rule is not None and rule._date_pattern is not None:
        for match in rule._date_pattern.finditer(window):
            captured = match[1]
            # Parsed by exactly the parser that scanned the window, over the capture alone.
            iso = _first_date(captured, date_order) if captured else None
            if iso:
                return (iso, "rule-regex")

    select = (rule.date_select if rule is not None else None) or "first"
    iso = _last_date(window, date_order) if select == "last" else _first_date(window, date_order)
    return (iso, select) if iso else (None, None)
