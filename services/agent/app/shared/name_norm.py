"""Shared business-name normalization (EC-1, EC-2).

Both workers and the lead's reconcile step use this module, so every part of
the agent compares names the same way.

    normalize_name("The Smith & Sons, L.L.C.")
        -> normalized="smith and sons llc", core="smith and sons", suffix_class="llc"
    compare_names("Smith & Sons Inc.", "Smith and Sons, Incorporated").label
        -> "normalized_equal"
    irs_name_control("Mark Hemlock", is_individual=True)
        -> "HEML"
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

from rapidfuzz import fuzz

# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

# Entity designators, as token sequences after normalization, mapped to a class.
# Inc and Corp share one class: states treat them as the same designator.
_SUFFIXES: dict[tuple[str, ...], str] = {
    ("llc",): "llc",
    ("lc",): "llc",
    ("limited", "liability", "company"): "llc",
    ("pllc",): "pllc",
    ("professional", "limited", "liability", "company"): "pllc",
    ("inc",): "corp",
    ("incorporated",): "corp",
    ("corp",): "corp",
    ("corporation",): "corp",
    ("co",): "co",
    ("company",): "co",
    ("ltd",): "ltd",
    ("limited",): "ltd",
    ("llp",): "llp",
    ("limited", "liability", "partnership"): "llp",
    ("lp",): "lp",
    ("limited", "partnership"): "lp",
    ("pc",): "pc",
    ("professional", "corporation"): "pc",
}
# Longest sequences first, so "limited liability company" wins over "company".
_SUFFIX_SEQS = sorted(_SUFFIXES, key=len, reverse=True)

# "Legal Name dba Trade Name". Needs text on both sides of the marker.
_DBA_MARKER = re.compile(
    r"\s+(?:d\s*/\s*b\s*/\s*a|d\.\s*b\.\s*a\.?|dba|doing\s+business\s+as"
    r"|t\s*/\s*a|trading\s+as|a\.\s*k\.\s*a\.?|aka)\s+",
    re.IGNORECASE,
)

# Name-suffix tokens on a person's name, skipped when picking the last name.
_PERSONAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq", "dds", "cpa"}

SuffixClass = Literal["llc", "pllc", "corp", "co", "ltd", "llp", "lp", "pc", "none"]
CompareLabel = Literal[
    "exact", "normalized_equal", "core_equal_suffix_differs", "similar", "different"
]

# Higher is a closer match.
LABEL_RANK: dict[str, int] = {
    "different": 0,
    "similar": 1,
    "core_equal_suffix_differs": 2,
    "normalized_equal": 3,
    "exact": 4,
}

SIMILAR_THRESHOLD = 90.0


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizedName:
    original: str
    normalized: str            # core + canonical suffix class, e.g. "smith and sons corp"
    core: str                  # no suffix, no leading "the", e.g. "smith and sons"
    suffix_class: SuffixClass  # "none" when there is no entity designator
    embedded_dba: Optional[str]  # trade name after a "dba" marker, as written


@dataclass(frozen=True)
class NameComparison:
    label: CompareLabel
    score: float  # RapidFuzz token_sort_ratio on the cores, 0-100

    def at_least(self, label: CompareLabel) -> bool:
        return LABEL_RANK[self.label] >= LABEL_RANK[label]


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def _ascii_fold(text: str) -> str:
    """NFKC, then drop accents: 'Café' -> 'Cafe'."""
    text = unicodedata.normalize("NFKC", text)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def split_dba(name: str) -> tuple[str, Optional[str]]:
    """Split 'Mark Hemlock dba Sunshine Cafe' into ('Mark Hemlock', 'Sunshine Cafe')."""
    parts = _DBA_MARKER.split(name.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return name.strip(), None


def _tokens(text: str) -> list[str]:
    text = _ascii_fold(text).casefold()
    text = text.replace("&", " and ").replace("+", " and ")
    # Periods and apostrophes join letters: "L.L.C." -> "llc", "Joe's" -> "joes".
    text = re.sub(r"[.'’`]", "", text)
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return text.split()


def _strip_suffixes(tokens: list[str]) -> tuple[list[str], SuffixClass]:
    """Remove trailing designators ("Acme Co., Inc." -> ["acme"]); keep at least one token."""
    suffix_class: SuffixClass = "none"
    changed = True
    while changed:
        changed = False
        for seq in _SUFFIX_SEQS:
            n = len(seq)
            if len(tokens) > n and tuple(tokens[-n:]) == seq:
                if suffix_class == "none":
                    suffix_class = _SUFFIXES[seq]  # the outermost designator wins
                tokens = tokens[:-n]
                changed = True
                break
        # "Smith & Co." leaves a dangling "and".
        if changed and len(tokens) > 1 and tokens[-1] == "and":
            tokens = tokens[:-1]
    return tokens, suffix_class


def normalize_name(name: str) -> NormalizedName:
    legal_part, embedded_dba = split_dba(name)
    tokens = _tokens(legal_part)
    core_tokens, suffix_class = _strip_suffixes(tokens)
    if len(core_tokens) > 1 and core_tokens[0] == "the":
        core_tokens = core_tokens[1:]

    core = " ".join(core_tokens)
    normalized = core if suffix_class == "none" else f"{core} {suffix_class}"
    return NormalizedName(
        original=name,
        normalized=normalized,
        core=core,
        suffix_class=suffix_class,
        embedded_dba=embedded_dba,
    )


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def compare_names(a: str, b: str) -> NameComparison:
    """Compare two names' legal parts. Labels, closest first:
    exact > normalized_equal > core_equal_suffix_differs > similar > different.
    """
    na, nb = normalize_name(a), normalize_name(b)
    score = float(fuzz.token_sort_ratio(na.core, nb.core))

    if a.strip() == b.strip():
        return NameComparison("exact", 100.0)
    if na.normalized and na.normalized == nb.normalized:
        return NameComparison("normalized_equal", 100.0)
    if na.core and na.core == nb.core:
        return NameComparison("core_equal_suffix_differs", 100.0)
    if score >= SIMILAR_THRESHOLD:
        return NameComparison("similar", score)
    return NameComparison("different", score)


def name_variants(name: str) -> list[str]:
    """The legal part, plus the trade name when the string embeds one."""
    legal, dba = split_dba(name)
    return [legal] if dba is None else [legal, dba]


def best_comparison(a: str, b: str) -> NameComparison:
    """Closest comparison across both names' legal and embedded-DBA variants."""
    results = [compare_names(x, y) for x in name_variants(a) for y in name_variants(b)]
    return max(results, key=lambda r: (LABEL_RANK[r.label], r.score))


def _get_dba_name(record: Any) -> Optional[str]:
    """The trade name of a DBA record: ``dba`` (``DbaFiling``) or ``dba_name`` (older dicts)."""
    if isinstance(record, Mapping):
        return record.get("dba") or record.get("dba_name")
    return getattr(record, "dba", None) or getattr(record, "dba_name", None)


def match_dba(
    name: str,
    dba_records: Iterable[Any],
    min_label: CompareLabel = "normalized_equal",
) -> list[Any]:
    """Return the DBA records (dicts or objects with ``dba`` or ``dba_name``) whose name matches.

    ``name`` may embed a trade name ("Mark Hemlock dba Sunshine Cafe"); both
    parts are tried.
    """
    matches = []
    for record in dba_records:
        dba_name = _get_dba_name(record)
        if dba_name and best_comparison(name, dba_name).at_least(min_label):
            matches.append(record)
    return matches


# --------------------------------------------------------------------------
# IRS name control (simplified IRS Publication 1346 rules)
# --------------------------------------------------------------------------

def _name_control_chars(text: str) -> str:
    """Upper-case; keep letters, digits, '&' and '-'; drop spaces and other punctuation."""
    text = _ascii_fold(text).upper()
    return re.sub(r"[^A-Z0-9&-]", "", text)


def _individual_last_name(name: str) -> str:
    name = _ascii_fold(split_dba(name)[0]).strip()  # "Mark Hemlock dba Sunshine Cafe" -> "Mark Hemlock"
    if "," in name:  # "Hemlock, Mark" or "Hemlock, Mark Jr"
        return name.split(",", 1)[0]
    words = [w for w in re.split(r"\s+", name) if w]
    while len(words) > 1 and re.sub(r"[^a-z]", "", words[-1].casefold()) in _PERSONAL_SUFFIXES:
        words.pop()
    return words[-1] if words else ""


def irs_name_control(name: str, is_individual: bool = False) -> str:
    """Up to 4 characters the IRS uses to match a name against a TIN.

    - Individual (SSN, sole proprietor): first 4 characters of the last name,
      hyphen kept ("Mark Hemlock" -> "HEML", "Ann Smith-Jones" -> "SMIT").
    - Business (EIN): first 4 characters of the name with spaces removed;
      '&' and '-' kept. A leading "The" is dropped when more than one word
      follows it ("The Hawthorn Bakery" -> "HAWT", "The Hawthorn" -> "THEH").
      An embedded DBA is ignored: only the legal part counts.

    Names shorter than 4 characters return fewer characters (the IRS does not pad).
    """
    if is_individual:
        return _name_control_chars(_individual_last_name(name))[:4]

    legal, _ = split_dba(name)
    words = [w for w in re.split(r"\s+", _ascii_fold(legal).strip()) if w]
    if len(words) > 2 and words[0].casefold() == "the":
        words = words[1:]
    return _name_control_chars("".join(words))[:4]


__all__ = [
    "LABEL_RANK",
    "NameComparison",
    "NormalizedName",
    "best_comparison",
    "compare_names",
    "irs_name_control",
    "match_dba",
    "name_variants",
    "normalize_name",
    "split_dba",
]
