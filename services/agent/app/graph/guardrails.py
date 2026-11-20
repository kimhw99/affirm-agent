"""Step 9: checks (code). Builds evidence_index, then enforces the hard rules.

1. ``evidence_index``: every ID the memo cites is looked up in ``CaseInput`` (D, B, W,
   K) or the workers' saved sources (R). Excerpts come from saved data, never from the
   LLM. An ID that can't be found is a CITATION_NOT_FOUND failure.
2. Hard rules, mostly for flags the Lead marked resolved:
   - CLEAR_NEEDS_TWO_SOURCES: a resolved flag cites an official/third-party record (R or K)
     *and* merchant evidence (D, B or W)
   - EIN_OTHER_ENTITY_NOT_ESCALATED: the EIN belongs to an unrelated company -> escalate
   - NOT_ISSUED_TIN_CLEARED: a TIN flag can't be resolved while the IRS says Not Issued
   - HARD_FAIL_NOT_ESCALATED: a blocking fact exists but the memo doesn't escalate
   - REQUIRED_CHECK_MISSING: every required template check is in the memo
   - DOCUMENT_ALREADY_IN_CASE: never ask for a document type the case already has
   - FULL_TIN_IN_MEMO: no full SSN or EIN anywhere
   - WEBSITE_OVERRULES_RECORDS: an explanation can't rest on the merchant's website alone
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.graph.facts import FactIndex, text
from app.graph.plan import Plan
from app.graph.reconcile import same_name, tin_results
from kyb_contracts.case_input import TIN_PATTERN, CaseInput, FlagReason
from kyb_contracts.memo import CheckFailure, CheckResult, EvidenceEntry, Memo

_DOC_LABEL = {
    "irs_cp575": "IRS CP 575",
    "irs_147c": "IRS 147C",
    "articles": "Articles of organization",
    "articles_of_amendment": "Articles of amendment",
    "dba_certificate": "DBA certificate",
    "w9": "W-9",
    "good_standing": "Certificate of good standing",
    "business_license": "Business license",
}
_DATE_KEYS = ("date", "filed", "issued", "formation_date")
_TIN_FLAGS = {FlagReason.TIN_MISMATCH, FlagReason.TIN_NOT_FOUND, FlagReason.TIN_NOT_ISSUED}


def _as_date(v) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v)[:10]).date() if v else None
    except ValueError:
        return None


def _case_entry(case: CaseInput, eid: str) -> Optional[EvidenceEntry]:
    for d in case.documents:
        if d.id == eid:
            fields = ", ".join(f"{k}: {v}" for k, v in d.extracted.items())
            as_of = next((_as_date(d.extracted[k]) for k in _DATE_KEYS if d.extracted.get(k)), None)
            return EvidenceEntry(source="Merchant upload", type="merchant_document", authority="merchant",
                                 locator=f"{case.case_id}/{d.id}",
                                 excerpt=f"{_DOC_LABEL.get(d.type, d.type)}: {fields}", as_of=as_of)
    b = case.bank_verification
    if b and b.id == eid:
        return EvidenceEntry(source="Bank verification", type="bank_verification", authority="third_party",
                             locator=f"{case.case_id}/{b.id}",
                             excerpt=f"Account holder: {b.account_holder} ({b.status})", as_of=b.verified_at)
    for w in case.web_snapshot:
        if w.id == eid:
            fields = ", ".join(f"{k}: {v}" for k, v in w.extracted.items()) or "no fields extracted"
            return EvidenceEntry(source=str(w.url), type="website", authority="merchant",
                                 locator=f"{case.case_id}/{w.id}", excerpt=fields, as_of=w.captured_at)
    for k in case.owner_kyc:
        if k.id == eid:
            return EvidenceEntry(source="Owner KYC vendor", type="owner_kyc", authority="third_party",
                                 locator=f"{case.case_id}/{k.id}", excerpt=f"{k.owner_ref}: KYC {k.result}")
    return None


def _record_entry(facts: FactIndex, eid: str) -> Optional[EvidenceEntry]:
    src = facts.sources.get(eid)
    if src is None:
        return None
    excerpt = next((f.excerpt for f in facts.facts if f.source_id == eid and f.excerpt), src.source_type)
    authority = src.authority if isinstance(src.authority, str) else src.authority.value
    return EvidenceEntry(
        source=src.source_type.replace("_", " "),
        type="official_record" if authority == "official" else "third_party_record",
        authority=authority, locator=src.locator, excerpt=excerpt,
        as_of=src.retrieved_at.date() if src.retrieved_at else None,
    )


def build_evidence_index(memo: Memo, case: Optional[CaseInput],
                         facts: Optional[FactIndex]) -> tuple[dict[str, EvidenceEntry], list[str]]:
    index: dict[str, EvidenceEntry] = {}
    missing: list[str] = []
    for eid in sorted(memo.cited_ids(), key=lambda x: (x[0], int(x[1:]))):
        entry = None
        if eid.startswith("R"):
            entry = _record_entry(facts, eid) if facts else None
        elif case is not None:
            entry = _case_entry(case, eid)
        if entry is None:
            missing.append(eid)
        else:
            index[eid] = entry
    return index, missing


def run_checks(memo: Memo, case: Optional[CaseInput], facts: Optional[FactIndex],
               plan: Optional[Plan], *, tin_redacted: bool = False,
               attempts: int = 1) -> tuple[CheckResult, dict[str, EvidenceEntry]]:
    index, missing = build_evidence_index(memo, case, facts)
    failures: list[CheckFailure] = []

    for eid in missing:
        failures.append(CheckFailure(rule="CITATION_NOT_FOUND", detail=f"{eid} is not in the case or the saved sources"))

    if tin_redacted or TIN_PATTERN.search(memo.model_dump_json()):
        failures.append(CheckFailure(rule="FULL_TIN_IN_MEMO",
                                     detail="The LLM wrote something shaped like a full SSN or EIN; it was redacted"))

    for f in memo.flags:
        if f.status != "resolved":
            continue
        has_record = any(e[0] in "RK" for e in f.evidence)
        has_merchant = any(e[0] in "DBW" for e in f.evidence)
        if not (has_record and has_merchant):
            failures.append(CheckFailure(
                rule="CLEAR_NEEDS_TWO_SOURCES", flag=f.flag,
                detail=f"Resolved with {'records only' if has_record else 'merchant evidence only'} "
                       f"({', '.join(f.evidence) or 'no evidence'}). Cite both an R/K record and D/B/W evidence."))

    if facts is not None and case is not None:
        # The EIN belongs to an unrelated company.
        separate = facts.records("separate_entity")
        other = [r for r in tin_results(facts, case) if r.result == "Match"
                 and any(same_name(text(s.value), r.name_tested) for s in separate)]
        if other and memo.recommendation != "escalate":
            failures.append(CheckFailure(rule="EIN_OTHER_ENTITY_NOT_ESCALATED",
                                         detail=f"The EIN matches '{other[-1].name_tested}', a separate company "
                                                f"({other[-1].fact.source_id})"))
        # Not Issued TIN can't be cleared.
        results = tin_results(facts, case)
        if results and results[-1].result in ("Not Issued", "Not Found", "Never Issued") \
                and not any(r.result == "Match" for r in results):
            for f in memo.flags:
                if f.status == "resolved" and FlagReason(f.flag) in _TIN_FLAGS:
                    failures.append(CheckFailure(rule="NOT_ISSUED_TIN_CLEARED", flag=f.flag,
                                                 detail="The IRS has not issued / has no record of the TIN"))
        # Blocking facts must escalate.
        blocking = [x for x in facts.records("kyc_result") if (text(x.value) or "").lower() == "fail"]
        blocking += [x for x in facts.evidence("tampered") if str(x.value).lower() in ("true", "yes", "1")]
        if blocking and memo.recommendation != "escalate":
            failures.append(CheckFailure(rule="HARD_FAIL_NOT_ESCALATED",
                                         detail="Blocking finding(s) " + ", ".join(b.source_id for b in blocking)))
    if any(c.status == "HARD_FAIL" for c in memo.checks) and memo.recommendation != "escalate":
        failures.append(CheckFailure(rule="HARD_FAIL_NOT_ESCALATED", detail="A check is HARD_FAIL"))

    if plan is not None and memo.route == "reviewed":
        present = {c.check_id for c in memo.checks}
        for cid in plan.required_ids():
            if cid not in present:
                failures.append(CheckFailure(rule="REQUIRED_CHECK_MISSING", detail=f"{cid} is not in the memo"))

    if case is not None:
        have = {d.type for d in case.documents}
        for req in memo.document_request:
            if req.document_type in have:
                failures.append(CheckFailure(rule="DOCUMENT_ALREADY_IN_CASE",
                                             detail=f"The case already has a {req.document_type}"))

    for c in memo.checks:
        if c.status == "EXPLAINED" and c.explanation_evidence and all(e.startswith("W") for e in c.explanation_evidence):
            failures.append(CheckFailure(rule="WEBSITE_OVERRULES_RECORDS",
                                         detail=f"{c.check_id} is explained by the website alone"))

    result = CheckResult(passed=not failures, failures=failures, attempts=attempts)
    return result, index


__all__ = ["build_evidence_index", "run_checks"]
