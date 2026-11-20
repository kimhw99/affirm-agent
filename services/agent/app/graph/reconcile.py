"""Step 6: reconcile records against evidence, one check at a time.

Comparison happens in code first: exact EINs, normalized names, dates. Every check
gets one status:

    AGREE      both sides found the same thing (after normalization)
    EXPLAINED  they differ, but a cited, documented reason connects them
    GAP        one side has no finding
    CONFLICT   both sides found something and it doesn't match
    HARD_FAIL  blocks approval on its own; the LLM can't override it

The LLM is only asked whether a *difference* is EXPLAINED, and it has to cite the
findings that support the explanation. An explanation without a valid citation, or
one resting only on the merchant's website, stays a CONFLICT.

Trust ranking: official records > third-party documents (bank, IRS letters) > what
the merchant says about itself (website). A website can support a check but never
overrule the registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Optional, Protocol

from app.graph.facts import (
    EIN_LETTERS,
    RECORDS_FIELDS,
    FORMATION_DOCS,
    NAME_DOCS,
    Fact,
    FactIndex,
    as_date,
    cap_conf,
    ids,
    text,
    truthy,
    weakest_conf,
)
from app.graph.plan import EIN_LAG_DAYS, REGISTRY_LAG_DAYS, Plan, PlannedCheck
from app.shared.name_norm import best_comparison, irs_name_control, split_dba
from app.review import CheckOutcome

RECORDS_NAME_TESTED = RECORDS_FIELDS["name_tested"]

# ---------- Assessment ----------


@dataclass
class Assessment:
    status: str
    records_value: Any = None
    evidence_value: Any = None
    used: list[Fact] = field(default_factory=list)
    confidence: Optional[str] = None
    hard_fail_reason: Optional[str] = None
    explanation: Optional[str] = None
    explanation_evidence: list[str] = field(default_factory=list)
    explanation_facts: list[Fact] = field(default_factory=list)
    diff: Optional[str] = None                       # what differs, for the explainer
    candidates: list[Fact] = field(default_factory=list)  # facts that might explain it
    explainable: bool = True                         # False: never ask the LLM (e.g. hard data)


@dataclass
class Explanation:
    explanation: str
    cited: list[str]              # finding keys or evidence IDs


class Explainer(Protocol):
    def __call__(self, check: PlannedCheck, assessment: Assessment, facts: FactIndex) -> Optional[Explanation]: ...


# ---------- Small helpers ----------

ACTIVE = {"active", "good standing", "in good standing", "current", "good_standing"}
DEAD = {"dissolved", "revoked", "cancelled", "canceled", "forfeited", "terminated"}
PENDING = "pending"     # world.EntityStatus: filed with the state, not yet approved
MATCH = "Match"
DBA_DEAD = {"expired", "withdrawn"}     # world.DbaFiling.status values that no longer tie a DBA to anyone


def same_name(a: Optional[str], b: Optional[str], level: str = "normalized_equal") -> bool:
    if not a or not b:
        return False
    return best_comparison(a, b).at_least(level)


def legal_part(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return split_dba(name)[0]


def latest(facts: list[Fact]) -> Optional[Fact]:
    return facts[-1] if facts else None


def strong_and_weak(facts: Iterable[Fact]) -> tuple[list[Fact], list[Fact]]:
    """Split evidence into documents/bank (strong) and the merchant's website (weak)."""
    strong, weak = [], []
    for f in facts:
        (weak if f.source_kind == "website" else strong).append(f)
    return strong, weak


def former_names(facts: FactIndex) -> list[tuple[str, Fact]]:
    out: list[tuple[str, Fact]] = []
    for f in facts.records("former_name"):
        v = f.value
        if isinstance(v, dict):
            name = v.get("old_name") or v.get("name")
            if name:
                out.append((str(name), f))
        elif isinstance(v, list):
            out += [(str(n), f) for n in v if n]
        elif v:
            out.append((str(v), f))
    return out


@dataclass
class TinResult:
    result: str               # Match | Mismatch | Not Issued | Not Found | Never Issued | Error
    name_tested: Optional[str]
    fact: Fact


_LOCATOR_NAME = re.compile(r"\bname=(.+)$")
_NOTE_NAME = re.compile(r"^name tested: (.+)$")

_IRS_CODES = {"0": MATCH, "6": MATCH, "7": MATCH, "8": MATCH,
              "1": "Error", "2": "Not Issued", "3": "Mismatch", "4": "Error", "5": "Error"}
_QUOTED = re.compile(r"['\"]([^'\"]{2,})['\"]")


def _norm_result(raw: Any) -> str:
    s = str(raw).strip()
    if s in _IRS_CODES:
        return _IRS_CODES[s]
    low = s.lower().replace("_", " ")
    if "never" in low:
        return "Never Issued"
    if "not issued" in low or "not currently issued" in low:
        return "Not Issued"
    if "not found" in low:
        return "Not Found"
    if "mismatch" in low or "no match" in low or "does not match" in low:
        return "Mismatch"
    if "match" in low:
        return MATCH
    return "Error"


def _name_tested(f: Fact, facts: FactIndex) -> Optional[str]:
    """The name a TIN match was run with: the value itself, the source's locator
    ("tin-record:<case> name=<name>"), its name_control_tested note, or a quoted name."""
    if isinstance(f.value, dict):
        name = f.value.get("name_tested") or f.value.get("name")
        if name:
            return str(name)
    src = facts.sources.get(f.source_id)
    m = _LOCATOR_NAME.search(getattr(src, "locator", "") or "")
    if m:
        return m.group(1).strip()
    for g in facts.facts:
        if g.source_id == f.source_id and g.field in RECORDS_NAME_TESTED and g.note:
            m = _NOTE_NAME.match(g.note)
            if m:
                return m.group(1).strip()
    m = _QUOTED.search(f.excerpt or "") or _QUOTED.search(f.note or "")
    return m.group(1) if m else None


def tin_results(facts: FactIndex, case) -> list[TinResult]:
    out = []
    for f in facts.records("tin_match"):
        v = f.value
        result = _norm_result(v.get("result", v.get("code", "")) if isinstance(v, dict) else v)
        name = _name_tested(f, facts)
        if not name:
            if f.subject.startswith("owner:"):
                owner = next((o for o in case.application.owners if o.ref == f.subject), None)
                name = owner.name if owner else None
            else:
                name = case.application.legal_name
        out.append(TinResult(result, name, f))
    return out


def ein_letters(facts: FactIndex) -> dict[str, dict[str, Fact]]:
    """Per EIN letter (D id): its name, ein_last4, name_control and date facts."""
    out: dict[str, dict[str, Fact]] = {}
    for key in ("name", "ein_last4", "name_control", "date"):
        for f in facts.evidence(key, kinds=EIN_LETTERS):
            out.setdefault(f.source_id, {})[key] = f
    return out


def is_individual(case) -> bool:
    return case.application.entity_type == "SOLE_PROPRIETORSHIP" or case.application.tin_type == "SSN"


def owner_names(case) -> list[str]:
    return [o.name for o in case.application.owners]


# ---------- Comparators (one per template check) ----------

Comparator = Callable[["Ctx"], Assessment]


@dataclass
class Ctx:
    case: Any
    check: PlannedCheck
    facts: FactIndex
    today: date

    @property
    def app(self):
        return self.case.application


def _registry_name(ctx: Ctx) -> tuple[Optional[str], list[Fact]]:
    names = ctx.facts.records("legal_name")
    f = latest(names)
    return (text(f.value), [f]) if f else (None, [])


def _ev_names(ctx: Ctx) -> list[Fact]:
    """Legal names the merchant's evidence shows (not the EIN letter: that's EC-2's)."""
    docs = ctx.facts.evidence("name", kinds=NAME_DOCS | {"website"})
    holders = ctx.facts.evidence("account_holder")
    return docs + holders


def _name_check(ctx: Ctx, ref_name: Optional[str], ref_facts: list[Fact], ev: list[Fact],
                records_value: Any) -> Assessment:
    """Compare a reference (records) name with evidence names; the website can't overrule."""
    if not ev:
        return Assessment("GAP", records_value, None, ref_facts, "low",
                          diff="no merchant evidence names the business")
    strong, weak = strong_and_weak(ev)
    deciding = strong or weak
    mismatched = [f for f in deciding if not same_name(ref_name, legal_part(text(f.value)))]
    ev_value = "; ".join(dict.fromkeys(text(f.value) or "" for f in deciding))
    used = ref_facts + deciding
    conf = weakest_conf(used)
    if not strong:
        conf = cap_conf(conf, "low")          # website alone only supports
    if not mismatched:
        return Assessment("AGREE", records_value, ev_value, used, conf)
    return Assessment(
        "CONFLICT", records_value, ev_value, used, "low",
        diff=(f"records name '{ref_name}' vs evidence "
              + ", ".join(f"'{text(f.value)}' ({f.source_id})" for f in mismatched)),
        candidates=[f for _, f in former_names(ctx.facts)]
        + ctx.facts.records("dba_owner") + ctx.facts.evidence("old_name"),
    )


def cmp_entity_active(ctx: Ctx) -> Assessment:
    status_f = latest(ctx.facts.records("entity_status"))
    reg_name, name_facts = _registry_name(ctx)
    if not status_f and not reg_name:
        return Assessment("GAP", None, None, [], "low", diff="no registry entity found")
    status = (text(status_f.value) or "").lower() if status_f else ""
    ref_facts = name_facts + ([status_f] if status_f else [])
    records_value = ", ".join(x for x in (reg_name, status) if x)
    if status in DEAD:
        return Assessment("HARD_FAIL", records_value, None, [status_f], status_f.confidence,
                          hard_fail_reason="entity_dissolved_or_revoked", explainable=False)
    a = _name_check(ctx, reg_name or ctx.app.legal_name, ref_facts, _ev_names(ctx), records_value)
    if a.status == "AGREE" and status and status not in ACTIVE:
        a.status, a.confidence = "CONFLICT", "low"
        a.diff = f"registry status is '{status}', not active"
    return a


def cmp_name_normalized(ctx: Ctx) -> Assessment:
    reg_name, name_facts = _registry_name(ctx)
    if not reg_name:
        return Assessment("GAP", None, None, [], "low", diff="no registry name found")
    return _name_check(ctx, reg_name, name_facts, _ev_names(ctx), reg_name)


def cmp_bank_account_holder(ctx: Ctx) -> Assessment:
    bank = ctx.facts.evidence("account_holder")
    if not bank:
        return Assessment("GAP", None, None, [], "low", diff="no bank account holder found")
    holder_f = latest(bank)
    holder = text(holder_f.value) or ""
    reg_name, name_facts = _registry_name(ctx)
    legal_ref = reg_name or ctx.app.legal_name
    refs = [legal_ref, ctx.app.legal_name]
    if is_individual(ctx.case):
        refs += owner_names(ctx.case)
    legal, dba = split_dba(holder)
    ok_legal = any(same_name(r, legal) for r in refs)
    ok_dba = dba is None or (ctx.app.dba is not None and same_name(ctx.app.dba, dba))
    status_f = latest(ctx.facts.evidence("bank_status", kinds={"bank"}))
    used = name_facts + [holder_f] + ([status_f] if status_f else [])
    conf = weakest_conf(used)
    if status_f and (text(status_f.value) or "").lower() not in {"verified"}:
        conf = "low"
    if ok_legal and ok_dba:
        return Assessment("AGREE", reg_name, holder, used, conf)
    return Assessment(
        "CONFLICT", reg_name, holder, used, "low",
        diff=f"bank account holder '{holder}' vs legal name '{legal_ref}'"
             + (f" DBA '{ctx.app.dba}'" if ctx.app.dba else ""),
        candidates=[f for _, f in former_names(ctx.facts)] + ctx.facts.records("dba_owner"),
    )


def cmp_dba_owner(ctx: Ctx, owner_ref_names: Optional[list[str]] = None) -> Assessment:
    dba = ctx.app.dba
    filings = [f for f in ctx.facts.records("dba_owner")]
    reg_name, name_facts = _registry_name(ctx)
    expected_owners = owner_ref_names or [n for n in (reg_name, ctx.app.legal_name) if n]
    # Evidence: the DBA certificate registrant, the 'Legal DBA X' bank line, the website.
    ev: list[Fact] = list(ctx.facts.evidence("registrant", kinds={"dba_certificate"}))
    for f in ctx.facts.evidence("account_holder"):
        legal, trade = split_dba(text(f.value) or "")
        if trade and dba and same_name(trade, dba):
            ev.append(f)
    web_dba = ctx.facts.evidence("dba", kinds={"website"})
    if web_dba and any(same_name(text(f.value), dba) for f in web_dba):
        ev += ctx.facts.evidence("name", kinds={"website"})
    if not filings:
        return Assessment("GAP", None, _ev_value(ev), ev, "low",
                          diff=f"no DBA filing found for '{dba}'")
    # Only a live filing ties the DBA to its registrant. If every filing found is expired or
    # withdrawn, that's hard data from the official record: a CONFLICT the LLM can't explain away.
    dead_facts = [f for f in ctx.facts.records("dba_expired") if truthy(f.value)]
    dead_facts += [f for f in ctx.facts.records("dba_status") if (text(f.value) or "").lower() in DBA_DEAD]
    dead = {f.source_id for f in dead_facts}
    live = [f for f in filings if f.source_id not in dead]
    if not live:
        filing = latest(filings)
        status_facts = [f for f in dead_facts if f.source_id == filing.source_id]
        return Assessment(
            "CONFLICT", f"{dba}, filed by {text(filing.value)} (expired or withdrawn)", _ev_value(ev),
            [filing] + status_facts[-1:] + ev, "low",
            diff=f"the only DBA filing found for '{dba}' is expired or withdrawn", explainable=False,
        )
    filings = live
    filing = latest(filings)
    owner = text(filing.value)
    records_value = f"{dba}, filed by {owner}"
    # A DBA filing that names the applicant's entity number belongs to it, whatever name it was filed under.
    entity_id = (ctx.app.state_entity_id or "").strip().upper()
    by_number = [f for f in ctx.facts.records("dba_owner_entity_id")
                 if entity_id and owner_ref_names is None and f.source_id not in dead
                 and (text(f.value) or "").strip().upper() == entity_id]
    if not ev:
        return Assessment("GAP", records_value, None, [filing], "low",
                          diff="no merchant evidence ties the DBA to an entity")
    strong, weak = strong_and_weak(ev)
    deciding = strong or weak
    ev_owners = [legal_part(text(f.value)) for f in deciding]
    owner_ok = any(same_name(owner, n) for n in expected_owners) or bool(by_number)
    ev_ok = all(any(same_name(e, n) for n in expected_owners) for e in ev_owners)
    used = [filing] + by_number[-1:] + name_facts + deciding
    conf = weakest_conf(used)
    if not strong:
        conf = cap_conf(conf, "low")
    if owner_ok and ev_ok:
        return Assessment("AGREE", records_value, _ev_value(deciding), used, conf)
    return Assessment(
        "CONFLICT", records_value, _ev_value(deciding), used, "low",
        diff=f"DBA filed by '{owner}', evidence ties it to {ev_owners}; expected one of {expected_owners}",
        candidates=[f for _, f in former_names(ctx.facts)] + ctx.facts.evidence("old_name"),
    )


def _ev_value(facts: list[Fact]) -> Optional[str]:
    vals = [text(f.value) for f in facts if text(f.value)]
    return "; ".join(dict.fromkeys(vals)) or None


def cmp_tin_match(ctx: Ctx) -> Assessment:
    results = tin_results(ctx.facts, ctx.case)
    letters = ein_letters(ctx.facts)
    letter_names = [d["name"] for d in letters.values() if "name" in d]
    letter_dates = [d["date"] for d in letters.values() if "date" in d]
    if not results:
        return Assessment("GAP", None, _ev_value(letter_names), letter_names, "low",
                          diff="no TIN match result")
    applied = owner_names(ctx.case) if is_individual(ctx.case) else [ctx.app.legal_name]
    if is_individual(ctx.case):
        applied.append(ctx.app.legal_name)
    records_value = "; ".join(f"{r.result} on {r.name_tested}" for r in results)
    rec_facts = [r.fact for r in results]

    on_applied = [r for r in results if r.result == MATCH and any(same_name(r.name_tested, n) for n in applied)]
    if on_applied:
        used = [on_applied[-1].fact] + letter_names
        return Assessment("AGREE", records_value, _ev_value(letter_names), used, weakest_conf(used, "high"))

    not_issued = [r for r in results if r.result in ("Not Issued", "Not Found", "Never Issued")]
    if not_issued and not any(r.result == MATCH for r in results):
        newest = max((as_date(f.value) for f in letter_dates if as_date(f.value)), default=None)
        recent = newest is not None and (ctx.today - newest).days <= EIN_LAG_DAYS
        if any(r.result == "Never Issued" for r in not_issued) or (newest is not None and not recent):
            return Assessment("HARD_FAIL", records_value, _ev_value(letter_names),
                              [not_issued[-1].fact] + letter_dates, "high",
                              hard_fail_reason="tin_never_issued", explainable=False)
        # A fresh EIN often isn't in IRS systems yet: one side has no confirmation.
        return Assessment("GAP", records_value, _ev_value(letter_names), rec_facts + letter_dates, "low",
                          diff="the IRS has no record of the TIN yet" + (" (EIN letter is recent)" if recent else ""),
                          explainable=False)

    others = [r for r in results if r.result == MATCH]
    separate = ctx.facts.records("separate_entity")
    for r in others:
        hit = [f for f in separate if same_name(text(f.value), r.name_tested)]
        if hit:
            return Assessment("HARD_FAIL", records_value, _ev_value(letter_names),
                              [r.fact] + hit + letter_names, "high",
                              hard_fail_reason="ein_belongs_to_other_entity", explainable=False)
    if not letter_names and not others:
        return Assessment("GAP", records_value, None, rec_facts, "low",
                          diff="the TIN doesn't match and there is no EIN letter to compare")
    return Assessment(
        "CONFLICT", records_value, _ev_value(letter_names), rec_facts + letter_names, "low",
        diff=(f"TIN match fails on the submitted name; "
              + (f"it matches '{others[-1].name_tested}'" if others else "no name matched")
              + (f"; the EIN letter shows {[text(f.value) for f in letter_names]}" if letter_names else "")),
        candidates=[f for _, f in former_names(ctx.facts)] + [r.fact for r in others]
        + ctx.facts.records("kyc_result") + ctx.facts.evidence("old_name"),
    )


def cmp_name_control(ctx: Ctx) -> Assessment:
    letters = ein_letters(ctx.facts)
    ev_controls: list[tuple[str, Fact]] = []
    for d in letters.values():
        if "name_control" in d and text(d["name_control"].value):
            ev_controls.append((text(d["name_control"].value).upper(), d["name_control"]))
        elif "name" in d and text(d["name"].value):
            ev_controls.append((irs_name_control(text(d["name"].value), is_individual(ctx.case)), d["name"]))
    if not ev_controls and is_individual(ctx.case):  # no EIN letter: the W-9 names the TIN holder
        for f in ctx.facts.evidence("name", kinds={"w9"}):
            if text(f.value):
                ev_controls.append((irs_name_control(text(f.value), True), f))
    rec_controls: list[tuple[str, Fact]] = [
        (text(f.value).upper(), f) for f in ctx.facts.records("name_control") if text(f.value)
    ]
    if not rec_controls:  # derive from the name the IRS accepted
        for r in tin_results(ctx.facts, ctx.case):
            if r.result == MATCH and r.name_tested:
                rec_controls.append((irs_name_control(r.name_tested, r.fact.subject.startswith("owner:")
                                                      or is_individual(ctx.case)), r.fact))
    if not rec_controls or not ev_controls:
        used = [f for _, f in rec_controls + ev_controls]
        return Assessment("GAP", rec_controls[-1][0] if rec_controls else None,
                          ev_controls[-1][0] if ev_controls else None, used, "low",
                          diff="no name control on one side")
    rec, rec_f = rec_controls[-1]
    ev_hit = [(c, f) for c, f in ev_controls if c == rec]
    if ev_hit:
        used = [rec_f, ev_hit[0][1]]
        conf = "medium" if rec_f.field not in {"irs_name_control", "name_control"} else weakest_conf(used)
        return Assessment("AGREE", rec, ev_hit[0][0], used, conf)
    ev, ev_f = ev_controls[-1]
    return Assessment(
        "CONFLICT", rec, ev, [rec_f, ev_f], "low",
        diff=f"IRS name control {rec} vs EIN letter {ev}",
        candidates=[f for _, f in former_names(ctx.facts)] + ctx.facts.records("tin_match"),
    )


def cmp_ein_letter_matches(ctx: Ctx) -> Assessment:
    submitted = ctx.app.tin_last4
    letters = ein_letters(ctx.facts)
    last4s = [d["ein_last4"] for d in letters.values() if "ein_last4" in d]
    rec_facts = ctx.facts.records("tin_match")[-1:]
    if not submitted or not last4s:
        return Assessment("GAP", submitted, _ev_value(last4s), rec_facts + last4s, "low",
                          diff="no EIN letter" if submitted else "no submitted TIN", explainable=False)
    bad = [f for f in last4s if (text(f.value) or "").strip()[-4:] != submitted]
    used = rec_facts + last4s
    if not bad:
        return Assessment("AGREE", submitted, submitted, used, weakest_conf(last4s, "high"), explainable=False)
    if all(f.confidence == "low" for f in bad):  # a shaky OCR read isn't proof of a different EIN
        return Assessment("CONFLICT", submitted, _ev_value(bad), used, "low",
                          diff="low-confidence EIN read differs from the submitted EIN", explainable=False)
    return Assessment("HARD_FAIL", submitted, _ev_value(bad), rec_facts + bad, "high",
                      hard_fail_reason="ein_letter_mismatch", explainable=False)


def cmp_tin_type_fits(ctx: Ctx) -> Assessment:
    app = ctx.app
    w9 = ctx.facts.evidence("tin_type", kinds={"w9"})
    ev_type = text(latest(w9).value).upper() if w9 else None
    tin_type = app.tin_type
    if tin_type is None:
        return Assessment("GAP", app.entity_type, ev_type, w9, "low", diff="no TIN type submitted",
                          explainable=False)
    if ev_type and ev_type != tin_type:
        return Assessment("CONFLICT", f"{app.entity_type} / {tin_type}", ev_type, w9, "low",
                          diff=f"application TIN type {tin_type}, W-9 says {ev_type}", explainable=False)
    fits = (tin_type == "EIN" or app.entity_type == "SOLE_PROPRIETORSHIP"
            or (app.entity_type == "LLC" and len(app.owners) == 1))
    value = f"{app.entity_type} / {tin_type}"
    if fits:
        return Assessment("AGREE", value, ev_type or tin_type, w9, weakest_conf(w9, "high"), explainable=False)
    return Assessment("CONFLICT", value, ev_type or tin_type, w9, "low",
                      diff=f"an {tin_type} doesn't fit a {app.entity_type}", explainable=False)


def cmp_registry_presence(ctx: Ctx) -> Assessment:
    found_f = latest(ctx.facts.records("registry_found"))
    status_f = latest(ctx.facts.records("entity_status"))
    reg_name, name_facts = _registry_name(ctx)
    formation = ctx.facts.evidence("formation_date", kinds=FORMATION_DOCS) + \
        ctx.facts.evidence("date", kinds=FORMATION_DOCS)
    # Applicant entity facts win over a search that came back empty (the entity may be
    # found by its number but not by name); look-alike entities never count.
    found = bool(status_f or reg_name) or (found_f is not None and truthy(found_f.value))
    if found:
        status = (text(status_f.value) or "").lower() if status_f else ""
        if status in DEAD:
            return Assessment("HARD_FAIL", status, None, [status_f], "high",
                              hard_fail_reason="entity_dissolved_or_revoked", explainable=False)
        used = name_facts + ([status_f] if status_f else []) + formation
        records_value = f"{reg_name or 'found'}, {status or 'status unknown'}"
        if status == PENDING:
            # The pending filing is the registry's own record: the formation is filed, not yet approved.
            if ctx.check.expected_absence:
                return Assessment("EXPLAINED", records_value, _ev_value(formation), used, "medium",
                                  explanation="The formation is filed and pending at the registry; "
                                              "a new business was expected in the plan",
                                  explanation_evidence=ids(used), explanation_facts=used, explainable=False)
            return Assessment("GAP", records_value, _ev_value(formation), used, "low",
                              diff="the registry filing is still pending", explainable=False)
        return Assessment("AGREE", records_value, _ev_value(formation), used, weakest_conf(used, "high"))
    rec_facts = [found_f] if found_f and not truthy(found_f.value) else []
    records_value = "not found"
    if ctx.check.expected_absence:
        if is_individual(ctx.case):
            support = rec_facts + ctx.facts.records("kyc_result") + ctx.facts.evidence("registrant") \
                + ctx.facts.evidence("account_holder")
            if support:
                return Assessment("EXPLAINED", records_value, _ev_value(support), support, "medium",
                                  explanation="Sole proprietors are not in the state registry; "
                                              "the absence was expected in the plan",
                                  explanation_evidence=ids(support), explanation_facts=support,
                                  explainable=False)
        recent = [f for f in formation if as_date(f.value) and (ctx.today - as_date(f.value)).days <= REGISTRY_LAG_DAYS]
        if recent:
            support = rec_facts + recent
            when = as_date(recent[-1].value).isoformat()
            return Assessment("EXPLAINED", records_value, _ev_value(recent), support, "medium",
                              explanation=f"Formed {when}, within the registry's filing lag; "
                                          "the absence was expected in the plan",
                              explanation_evidence=ids(support), explanation_facts=support,
                              explainable=False)
    if not formation:
        return Assessment("GAP", records_value, None, rec_facts, "low",
                          diff="not in the registry and no formation documents", explainable=False)
    return Assessment("CONFLICT", records_value, _ev_value(formation), rec_facts + formation, "low",
                      diff="formation documents exist but the registry has no entity and the "
                           "formation is not recent")


def cmp_ein_age(ctx: Ctx) -> Assessment:
    results = tin_results(ctx.facts, ctx.case)
    dates = [d["date"] for d in ein_letters(ctx.facts).values() if "date" in d]
    if any(r.result == MATCH for r in results):
        used = [r.fact for r in results if r.result == MATCH][-1:] + dates
        return Assessment("AGREE", "Match", _ev_value(dates), used, weakest_conf(used, "high"))
    if not dates:
        return Assessment("GAP", results[-1].result if results else None, None,
                          [r.fact for r in results], "low", diff="no CP 575 date", explainable=False)
    issued = as_date(latest(dates).value)
    formed = ctx.app.formation_date
    if issued and formed and issued < formed:
        return Assessment("CONFLICT", f"formed {formed}", f"EIN issued {issued}", dates, "low",
                          diff="the EIN was issued before the business was formed")
    # The TIN isn't confirmed by the IRS: a GAP until it is, however recent the letter.
    return Assessment("GAP", results[-1].result if results else None, str(issued),
                      [r.fact for r in results] + dates, "low",
                      diff="the IRS hasn't confirmed the new EIN yet", explainable=False)


def cmp_sole_prop_owner(ctx: Ctx) -> Assessment:
    kyc = ctx.facts.records("kyc_result")
    results = tin_results(ctx.facts, ctx.case)
    owners = owner_names(ctx.case)
    if any((text(f.value) or "").lower() == "fail" for f in kyc):
        bad = [f for f in kyc if (text(f.value) or "").lower() == "fail"]
        return Assessment("HARD_FAIL", "KYC fail", None, bad, "high",
                          hard_fail_reason="kyc_failed", explainable=False)
    owner_match = [r for r in results if r.result == MATCH and any(same_name(r.name_tested, o) for o in owners)]
    ev = ctx.facts.evidence("name", kinds={"w9"}) + ctx.facts.evidence("account_holder")
    ev_ok = [f for f in ev if any(same_name(legal_part(text(f.value)), o) for o in owners)]
    kyc_pass = [f for f in kyc if (text(f.value) or "").lower() == "pass"]
    records_value = "; ".join(filter(None, [
        f"KYC {text(latest(kyc).value)}" if kyc else None,
        f"TIN {owner_match[-1].result} on {owner_match[-1].name_tested}" if owner_match else
        (f"TIN {results[-1].result} on {results[-1].name_tested}" if results else None),
    ])) or None
    used = kyc_pass + [r.fact for r in owner_match[-1:]] + ev_ok
    if kyc_pass and owner_match and ev_ok:
        return Assessment("AGREE", records_value, _ev_value(ev_ok), used, weakest_conf(used))
    if not kyc or not results or not ev:
        return Assessment("GAP", records_value, _ev_value(ev), kyc + [r.fact for r in results] + ev, "low",
                          diff="owner KYC, TIN match on the owner, or owner-named evidence is missing")
    return Assessment("CONFLICT", records_value, _ev_value(ev), kyc + [r.fact for r in results] + ev, "low",
                      diff="the TIN or the evidence doesn't point to the owner",
                      candidates=[r.fact for r in results])


def cmp_sole_prop_dba(ctx: Ctx) -> Assessment:
    return cmp_dba_owner(ctx, owner_ref_names=owner_names(ctx.case) + [ctx.app.legal_name])


def cmp_operating(ctx: Ctx) -> Assessment:
    bank = [f for f in ctx.facts.evidence("bank_status", kinds={"bank"})
            if (text(f.value) or "").lower() == "verified"]
    lic = [f for f in ctx.facts.evidence("license_status", kinds={"business_license"})
           if (text(f.value) or "").lower() in ACTIVE]
    web = ctx.facts.evidence("name", kinds={"website"})        # a website snapshot that names the business
    dba_cert = ctx.facts.evidence("registrant", kinds={"dba_certificate"})
    signals = bank + lic + web + dba_cert
    value = ", ".join(filter(None, ["bank verified" if bank else None, "license active" if lic else None,
                                    "website" if web else None, "DBA filed" if dba_cert else None])) or None
    if bank and (lic or web or dba_cert):
        return Assessment("AGREE", None, value, signals, weakest_conf(bank + lic, "medium"), explainable=False)
    return Assessment("GAP", None, value, signals, "low",
                      diff="needs a verified bank account plus a license or a website", explainable=False)


def cmp_generic(ctx: Ctx) -> Assessment:
    """For checks the Lead added: compare the focus fields' values on both sides."""
    rec = [f for f in ctx.facts.facts if f.worker == "records" and f.field in ctx.check.records_focus]
    ev = [f for f in ctx.facts.facts if f.worker == "evidence" and f.field in ctx.check.evidence_focus]
    if not rec or not ev:
        return Assessment("GAP", _ev_value(rec), _ev_value(ev), rec + ev, "low", diff="one side has no finding")
    rv, evv = text(latest(rec).value), text(latest(ev).value)
    if same_name(rv, evv) or (rv or "").strip().lower() == (evv or "").strip().lower():
        return Assessment("AGREE", rv, evv, [latest(rec), latest(ev)], weakest_conf([latest(rec), latest(ev)]))
    return Assessment("CONFLICT", rv, evv, [latest(rec), latest(ev)], "low", diff=f"'{rv}' vs '{evv}'")


COMPARATORS: dict[str, Comparator] = {
    "EC1.entity_active": cmp_entity_active,
    "EC1.dba_owner": cmp_dba_owner,
    "EC1.name_normalized": cmp_name_normalized,
    "EC1.bank_account_holder": cmp_bank_account_holder,
    "EC2.tin_match": cmp_tin_match,
    "EC2.name_control": cmp_name_control,
    "EC2.ein_letter_matches": cmp_ein_letter_matches,
    "EC2.tin_type_fits": cmp_tin_type_fits,
    "EC4.registry_presence": cmp_registry_presence,
    "EC4.ein_age": cmp_ein_age,
    "EC4.sole_prop_owner": cmp_sole_prop_owner,
    "EC4.sole_prop_dba": cmp_sole_prop_dba,
    "EC4.operating": cmp_operating,
}


# ---------- Rule-based explainer (used when no LLM is configured) ----------

def rule_explainer(check: PlannedCheck, a: Assessment, facts: FactIndex) -> Optional[Explanation]:
    """Known, documented reasons for a difference. Each one cites the findings it rests on."""
    case = facts.case
    diff_text = (a.diff or "") + " " + str(a.evidence_value or "") + " " + str(a.records_value or "")
    formers = former_names(facts)
    for name, f in formers:
        if name and name.upper() in diff_text.upper() or any(same_name(name, n) for n in _quoted(diff_text)):
            cited = [f.key]
            match = [r for r in tin_results(facts, case) if r.result == MATCH and same_name(r.name_tested, name)]
            cited += [r.fact.key for r in match]
            return Explanation(f"'{name}' is a former legal name of the entity (official filing history)", cited)
    # A merchant-provided amendment is weaker, but still a documented reason.
    for f in facts.evidence("old_name", kinds={"articles_of_amendment"}):
        name = text(f.value)
        if name and name.upper() in diff_text.upper():
            return Explanation(f"Articles of amendment show '{name}' as the former name", [f.key])
    if check.check_id == "EC2.tin_match":
        exp = _registered_name_explains_tin(facts, case)
        if exp:
            return exp
    if is_individual(case) and check.check_id in ("EC2.tin_match", "EC2.name_control"):
        owners = owner_names(case)
        hits = [r for r in tin_results(facts, case)
                if r.result == MATCH and any(same_name(r.name_tested, o) for o in owners)]
        if hits:
            return Explanation("Sole proprietor: the TIN is the owner's and the IRS matches it on the "
                               "owner's name, not the business name", [hits[-1].fact.key])
    return None


def _registered_name_explains_tin(facts: FactIndex, case) -> Optional[Explanation]:
    """The IRS matches the TIN to the registered legal name of the entity at the submitted
    entity number, and the applied name is an assumed name (DBA) that entity filed.

    Both links must be official records: without the DBA filing, an applied name that
    differs from the entity whose EIN was submitted is exactly what EIN misuse looks like.
    """
    applied = case.application.legal_name
    registry = facts.records("legal_name")       # the applicant's entity only
    if not registry or not case.application.state_entity_id:
        return None
    for r in reversed(tin_results(facts, case)):
        if r.result != MATCH or same_name(r.name_tested, applied):
            continue
        reg = [g for g in registry if same_name(r.name_tested, text(g.value))]
        if not reg:
            continue
        dba = [g for g in facts.records("dba_name") if same_name(text(g.value), applied)]
        owners = [g for g in facts.records("dba_owner") if same_name(text(g.value), text(reg[-1].value))]
        owners += [g for g in facts.records("dba_owner_entity_id")
                   if (text(g.value) or "").upper() == case.application.state_entity_id.upper()]
        if dba and owners:
            return Explanation(
                f"The IRS matches the TIN to '{r.name_tested}', the registered legal name of entity "
                f"{case.application.state_entity_id}; the applied name '{applied}' is an assumed name (DBA) "
                f"that entity filed",
                [r.fact.key, reg[-1].key, dba[-1].key, owners[-1].key])
    return None


def _quoted(s: str) -> list[str]:
    return re.findall(r"'([^']{2,})'", s)


# ---------- Running reconcile ----------

def _resolve_citations(cited: list[str], facts: FactIndex) -> tuple[list[str], list[Fact]]:
    known = facts.known_evidence_ids()
    out_ids: list[str] = []
    out_facts: list[Fact] = []
    for c in cited:
        f = facts.by_key(c)
        if f:
            out_facts.append(f)
            out_ids.append(f.source_id)
        elif c in known:
            out_ids.append(c)
            out_facts += [x for x in facts.facts if x.source_id == c]
    return list(dict.fromkeys(out_ids)), out_facts


def apply_explanation(check: PlannedCheck, a: Assessment, facts: FactIndex,
                      exp: Optional[Explanation]) -> Assessment:
    """Accept an explanation only with valid citations, and not on the website alone."""
    if exp is None or not exp.explanation.strip():
        return a
    cited_ids, cited_facts = _resolve_citations(exp.cited, facts)
    if not cited_ids or all(i.startswith("W") for i in cited_ids):
        return a
    a.status = "EXPLAINED"
    a.explanation = exp.explanation.strip()
    a.explanation_evidence = cited_ids
    a.explanation_facts = cited_facts
    conf = weakest_conf(a.used + cited_facts, "medium")
    if not any(f.authority in ("official", "third_party") for f in cited_facts):
        conf = cap_conf(conf, "medium")
    a.confidence = conf
    return a


def _hard_fail_sweep(case, facts: FactIndex, plan: Plan, out: dict[str, Assessment]) -> None:
    """Facts that block approval wherever they show up: failed KYC, dissolution.

    (No worker reports document tampering: the case service only stores extracted fields
    and an extraction confidence, and a low confidence is a misread as often as an edit.)"""
    if not out:
        return
    first = next(iter(out))
    for f in facts.records("kyc_result"):
        if (text(f.value) or "").lower() == "fail" and not any(a.hard_fail_reason == "kyc_failed" for a in out.values()):
            target = "EC4.sole_prop_owner" if "EC4.sole_prop_owner" in out else first
            out[target] = Assessment("HARD_FAIL", "KYC fail", None, [f], "high",
                                     hard_fail_reason="kyc_failed", explainable=False)
    for f in facts.records("entity_status"):
        if (text(f.value) or "").lower() in DEAD and not any(
                a.hard_fail_reason == "entity_dissolved_or_revoked" for a in out.values()):
            target = next((c for c in ("EC1.entity_active", "EC4.registry_presence") if c in out), first)
            out[target] = Assessment("HARD_FAIL", text(f.value), None, [f], "high",
                                     hard_fail_reason="entity_dissolved_or_revoked", explainable=False)


def assess(case, plan: Plan, facts: FactIndex, today: date,
           explainer: Optional[Explainer] = None,
           only: Optional[Iterable[str]] = None) -> dict[str, Assessment]:
    """Compare every planned check (or only ``only``) and settle explainable differences."""
    explainer = explainer or rule_explainer
    targets = set(only) if only is not None else None
    out: dict[str, Assessment] = {}
    for check in plan.checks:
        if targets is not None and check.check_id not in targets:
            continue
        comparator = COMPARATORS.get(check.check_id, cmp_generic)
        a = comparator(Ctx(case, check, facts, today))
        if a.status == "CONFLICT" and a.explainable:
            try:
                exp = explainer(check, a, facts)
            except Exception:  # an LLM failure leaves the CONFLICT standing
                exp = None
            a = apply_explanation(check, a, facts, exp)
        out[check.check_id] = a
    if targets is None:
        _hard_fail_sweep(case, facts, plan, out)
    return out


def to_outcome(check: PlannedCheck, a: Assessment) -> CheckOutcome:
    evidence = ids(a.used)
    return CheckOutcome(
        check_id=check.check_id,
        edge_case=check.edge_case,
        description=check.description,
        origin=check.origin,
        required=check.required,
        expected_absence=check.expected_absence,
        records_value=a.records_value,
        evidence_value=a.evidence_value,
        status=a.status,
        confidence=a.confidence or weakest_conf(a.used),
        evidence=evidence,
        explanation=a.explanation if a.status == "EXPLAINED" else None,
        explanation_evidence=a.explanation_evidence if a.status == "EXPLAINED" else [],
        hard_fail_reason=a.hard_fail_reason if a.status == "HARD_FAIL" else None,
    )


def reconcile(case, plan: Plan, facts: FactIndex, today: date,
              explainer: Optional[Explainer] = None,
              only: Optional[Iterable[str]] = None,
              previous: Optional[dict[str, CheckOutcome]] = None) -> tuple[dict[str, CheckOutcome], dict[str, Assessment]]:
    """Reconcile all checks, or re-reconcile only ``only`` and keep the rest from ``previous``."""
    assessments = assess(case, plan, facts, today, explainer, only)
    outcomes = dict(previous or {})
    for check in plan.checks:
        if check.check_id in assessments:
            outcomes[check.check_id] = to_outcome(check, assessments[check.check_id])
    if only is not None:  # hard-fail facts found by a follow-up still block approval
        current = {cid: _from_outcome(o) for cid, o in outcomes.items()}
        _hard_fail_sweep(case, facts, plan, current)
        for cid, a in current.items():
            if a.status == "HARD_FAIL" and outcomes[cid].status != "HARD_FAIL":
                outcomes[cid] = to_outcome(plan.check(cid), a)
                assessments[cid] = a
    # Keep plan order.
    return {c.check_id: outcomes[c.check_id] for c in plan.checks if c.check_id in outcomes}, assessments


def _from_outcome(o: CheckOutcome) -> Assessment:
    return Assessment(o.status, o.records_value, o.evidence_value, [], o.confidence,
                      hard_fail_reason=o.hard_fail_reason)


# ---------- Decision rules (applied in order) ----------

def decide(outcomes: dict[str, CheckOutcome]) -> str:
    """'hard_fail' -> escalate; 'follow_up' -> targeted follow-up; 'clear' -> recommend clear."""
    values = list(outcomes.values())
    if any(o.status == "HARD_FAIL" for o in values):
        return "hard_fail"
    if any(o.required and o.status in ("CONFLICT", "GAP") for o in values):
        return "follow_up"
    return "clear"


def open_required(outcomes: dict[str, CheckOutcome]) -> list[CheckOutcome]:
    return [o for o in outcomes.values() if o.required and o.status in ("CONFLICT", "GAP")]


__all__ = [
    "COMPARATORS",
    "Assessment",
    "Ctx",
    "Explainer",
    "Explanation",
    "apply_explanation",
    "assess",
    "decide",
    "former_names",
    "open_required",
    "reconcile",
    "rule_explainer",
    "same_name",
    "tin_results",
    "to_outcome",
]
