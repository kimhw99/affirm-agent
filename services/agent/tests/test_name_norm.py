"""Table-driven tests for app.shared.name_norm."""

import pytest

from app.shared.name_norm import (
    best_comparison,
    compare_names,
    irs_name_control,
    match_dba,
    normalize_name,
    split_dba,
)


# ---------------------------------------------------------------- normalize_name

@pytest.mark.parametrize(
    "raw, normalized, core, suffix_class",
    [
        # suffix variants
        ("Acme LLC", "acme llc", "acme", "llc"),
        ("Acme, L.L.C.", "acme llc", "acme", "llc"),
        ("Acme Limited Liability Company", "acme llc", "acme", "llc"),
        ("Acme Inc.", "acme corp", "acme", "corp"),
        ("Acme Incorporated", "acme corp", "acme", "corp"),
        ("ACME CORPORATION", "acme corp", "acme", "corp"),
        ("Acme Ltd", "acme ltd", "acme", "ltd"),
        ("Acme LLP", "acme llp", "acme", "llp"),
        ("Acme L.P.", "acme lp", "acme", "lp"),
        ("Acme PLLC", "acme pllc", "acme", "pllc"),
        ("Acme P.C.", "acme pc", "acme", "pc"),
        ("Acme", "acme", "acme", "none"),
        # stacked designators: the outermost one sets the class
        ("Acme Co., Inc.", "acme corp", "acme", "corp"),
        ("Smith & Co.", "smith co", "smith", "co"),
        # & vs and, punctuation, whitespace, case, accents
        ("Smith & Sons", "smith and sons", "smith and sons", "none"),
        ("Smith  and   Sons", "smith and sons", "smith and sons", "none"),
        ("Joe's Pizza", "joes pizza", "joes pizza", "none"),
        ("A-1 Plumbing", "a 1 plumbing", "a 1 plumbing", "none"),
        ("Café Olé LLC", "cafe ole llc", "cafe ole", "llc"),
        # leading "The"
        ("The Smith & Sons, L.L.C.", "smith and sons llc", "smith and sons", "llc"),
        # a lone designator is the name, not a suffix
        ("Company", "company", "company", "none"),
    ],
)
def test_normalize_name(raw, normalized, core, suffix_class):
    n = normalize_name(raw)
    assert (n.normalized, n.core, n.suffix_class) == (normalized, core, suffix_class)


@pytest.mark.parametrize(
    "raw, legal, dba",
    [
        ("Mark Hemlock dba Sunshine Cafe", "Mark Hemlock", "Sunshine Cafe"),
        ("Mark Hemlock DBA Sunshine Cafe", "Mark Hemlock", "Sunshine Cafe"),
        ("Hemlock Holdings LLC d/b/a Sunshine Cafe", "Hemlock Holdings LLC", "Sunshine Cafe"),
        ("Hemlock Holdings LLC d.b.a. Sunshine Cafe", "Hemlock Holdings LLC", "Sunshine Cafe"),
        ("Hemlock Holdings LLC doing business as Sunshine Cafe", "Hemlock Holdings LLC", "Sunshine Cafe"),
        ("Hemlock Holdings t/a Sunshine Cafe", "Hemlock Holdings", "Sunshine Cafe"),
        ("Sunshine Cafe", "Sunshine Cafe", None),
        # a marker word at the start is part of the name
        ("Aka Sushi", "Aka Sushi", None),
    ],
)
def test_split_dba(raw, legal, dba):
    assert split_dba(raw) == (legal, dba)


def test_normalize_name_keeps_embedded_dba():
    n = normalize_name("Hemlock Holdings, LLC dba Sunshine Cafe")
    assert n.core == "hemlock holdings"
    assert n.suffix_class == "llc"
    assert n.embedded_dba == "Sunshine Cafe"


# ---------------------------------------------------------------- compare_names

@pytest.mark.parametrize(
    "a, b, label",
    [
        ("Sunshine Cafe", "Sunshine Cafe", "exact"),
        ("Smith & Sons Inc.", "Smith and Sons, Incorporated", "normalized_equal"),
        ("Acme L.L.C.", "ACME LLC", "normalized_equal"),
        ("The Acme Corp", "Acme Corporation", "normalized_equal"),
        ("Acme Inc", "Acme Corp", "normalized_equal"),  # same designator class
        ("Acme LLC", "Acme Inc", "core_equal_suffix_differs"),
        ("Sunshine Cafe LLC", "Sunshine Cafe", "core_equal_suffix_differs"),
        ("Sunshine Cafe", "Sunshine Caffe", "similar"),
        ("Bright Horizons Bakery", "Horizons Bright Bakery", "similar"),  # word order
        ("Sunshine Cafe", "Sunrise Diner", "different"),
        ("Sunshine Cafe", "Mark Hemlock", "different"),
    ],
)
def test_compare_names(a, b, label):
    result = compare_names(a, b)
    assert result.label == label
    assert 0.0 <= result.score <= 100.0


def test_compare_names_is_symmetric():
    pairs = [("Smith & Sons Inc.", "Smith and Sons, Incorporated"), ("Acme LLC", "Acme Inc")]
    for a, b in pairs:
        assert compare_names(a, b).label == compare_names(b, a).label


def test_best_comparison_uses_embedded_dba():
    assert compare_names("Mark Hemlock dba Sunshine Cafe", "Sunshine Cafe").label == "different"
    assert best_comparison("Mark Hemlock dba Sunshine Cafe", "Sunshine Cafe").label == "exact"


# ---------------------------------------------------------------- match_dba

DBA_RECORDS = [
    {"filing_id": "nassau_1", "dba_name": "Sunshine Cafe", "owner_name": "Mark Hemlock"},
    {"filing_id": "nassau_2", "dba_name": "Sunshine Café & Bakery", "owner_name": "Ann Lee"},
    {"filing_id": "nassau_3", "dba_name": "Sunrise Diner", "owner_name": "Bob Ray"},
]


def test_match_dba_exact_and_normalized():
    hits = match_dba("SUNSHINE CAFE", DBA_RECORDS)
    assert [r["filing_id"] for r in hits] == ["nassau_1"]


def test_match_dba_with_embedded_trade_name():
    hits = match_dba("Mark Hemlock d/b/a Sunshine Cafe", DBA_RECORDS)
    assert [r["filing_id"] for r in hits] == ["nassau_1"]


def test_match_dba_no_match():
    assert match_dba("Moonlight Bistro", DBA_RECORDS) == []


def test_match_dba_looser_threshold():
    hits = match_dba("Sunshine Cafe and Bakery LLC", DBA_RECORDS, min_label="core_equal_suffix_differs")
    assert [r["filing_id"] for r in hits] == ["nassau_2"]


def test_match_dba_reads_the_dba_field_of_a_dba_filing():
    from kyb_contracts.world import DbaFiling

    filing = DbaFiling(filing_id="X-1", state="NY", county="Nassau", dba="SUNSHINE CAFE",
                       registrant="MARK HEMLOCK", registrant_type="individual",
                       filed="2023-04-20", status="active")
    assert match_dba("Sunshine Cafe", [filing]) == [filing]
    assert match_dba("Sunshine Café", [{"dba": "Sunshine Cafe"}]) == [{"dba": "Sunshine Cafe"}]


def test_match_dba_accepts_objects():
    class Rec:
        def __init__(self, dba_name):
            self.dba_name = dba_name

    recs = [Rec("Sunshine Cafe"), Rec(None)]
    assert match_dba("Sunshine Cafe", recs) == [recs[0]]


# ---------------------------------------------------------------- irs_name_control

@pytest.mark.parametrize(
    "name, is_individual, expected",
    [
        # individuals / sole proprietors: last name
        ("Mark Hemlock", True, "HEML"),
        ("Hemlock, Mark", True, "HEML"),
        ("Mark A. Hemlock Jr.", True, "HEML"),
        ("Mark Hemlock dba Sunshine Cafe", True, "HEML"),  # embedded DBA dropped first
        ("Ann Smith-Jones", True, "SMIT"),
        ("Ann Al-Rashid", True, "AL-R"),
        ("Jose Nunez", True, "NUNE"),
        ("Tom Ng", True, "NG"),  # short names are not padded
        # businesses
        ("Sunshine Cafe", False, "SUNS"),
        ("Y & Z Company", False, "Y&ZC"),
        ("A-1 Plumbing LLC", False, "A-1P"),
        ("The Hawthorn Bakery", False, "HAWT"),  # "The" dropped: 2+ words follow
        ("The Hawthorn", False, "THEH"),         # "The" kept: one word follows
        ("St. Mary's Clinic", False, "STMA"),
        ("Hemlock Holdings LLC dba Sunshine Cafe", False, "HEML"),  # legal part only
    ],
)
def test_irs_name_control(name, is_individual, expected):
    assert irs_name_control(name, is_individual) == expected


def test_sole_prop_name_control_differs_from_trade_name():
    """EC-2: a sole proprietor's TIN is on the owner, so the trade name's control differs."""
    assert irs_name_control("Sunshine Cafe") != irs_name_control("Mark Hemlock", is_individual=True)
