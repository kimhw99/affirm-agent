"""Step 9: checks (code). Builds evidence_index, then enforces the hard rules.

1. ``evidence_index`` (memo.EvidenceEntry): every ID the memo cites is looked up in
   ``CaseInput`` (D, B, W, K) or the workers' saved sources (R). Excerpts come from
   saved data, never from the LLM. IDs the Lead's report cites but the memo doesn't
   get the same treatment, in a separate index.
2. The memo's hard rules (memo.CheckRule), reported in memo.CheckResult:
   - CITED_ID_MUST_EXIST: every cited ID is in the case or the workers' sources
   - CLEAR_NEEDS_TWO_SOURCES: a resolved flag cites an official/third-party record (R or K)
     *and* merchant evidence (D, B or W)
   - UNRELATED_EIN_MUST_ESCALATE: the EIN belongs to an unrelated company -> escalate
   - NOT_ISSUED_TIN_CANT_CLEAR: a TIN flag can't be resolved while the IRS says Not Issued
   - NO_FULL_TAX_ID: no full SSN or EIN anywhere
3. The Lead's own rules (review.LeadRule), reported beside it:
   - HARD_FAIL_NOT_ESCALATED: a blocking fact exists but the memo doesn't escalate
   - REQUIRED_CHECK_MISSING: every required template check is in the report
   - DOCUMENT_ALREADY_IN_CASE: never ask for a document type the case already has
   - WEBSITE_OVERRULES_RECORDS: an explanation can't rest on the merchant's website alone

Any failure sends the memo back once (workflow.after_checks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from app.graph.facts import FactIndex, text
from app.graph.plan import Plan
from app.graph.reconcile import same_name, tin_results
from kyb_contracts.case_input import TIN_PATTERN, CaseInput, FlagReason
from kyb_contracts.memo import CheckFailure, CheckResult, EvidenceEntry, Memo
from kyb_contracts.review import LeadCheckFailure, LeadReport

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
_SOURCE_LABEL = {
    "sos_registry": "Secretary of State registry",
    "sos_registry_search": "Secretary of State registry search",
    "sos_filing_history": "Secretary of State filing history",
    "county_dba": "County DBA records",
    "state_dba": "State DBA records",
    "dba_search": "DBA records search",
    "irs_tin_match": "IRS TIN match",
    "commercial_db": "Commercial database",
    "commercial_db_search": "Commercial database search",
}
_MAX_EXCERPT_LINES = 4


def _as_date(v) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(v)[:10]).date() if v else None
    except ValueError:
        return None


def _case_entry(case: CaseInput, eid: str) -> Optional[EvidenceEntry]:
    for d in case.documents:
        if d.id == eid:
            fields = ", ".join(f"{k}: {v}" for k, v in d.extracted.items()) or "no fields extracted"
            as_of = next((_as_date(d.extracted[k]) for k in _DATE_KEYS if d.extracted.get(k)), None)
            return EvidenceEntry(source="Merchant upload", type="merchant_document",
                                 excerpt=f"{_DOC_LABEL.get(d.type, d.type)}: {fields}", as_of=as_of)
    b = case.bank_verification
    if b and b.id == eid:
        return EvidenceEntry(source="Bank verification", type="bank_verification",
                             excerpt=f"Account holder: {b.account_holder} ({b.status})", as_of=b.verified_at)
    for w in case.web_snapshot:
        if w.id == eid:
            fields = ", ".join(f"{k}: {v}" for k, v in w.extracted.items()) or "no fields extracted"
            return EvidenceEntry(source=str(w.url), type="website", excerpt=fields, as_of=w.captured_at)
    for k in case.owner_kyc:
        if k.id == eid:
            return EvidenceEntry(source="Owner KYC vendor", type="owner_kyc", excerpt=f"{k.owner_ref}: KYC {k.result}")
    return None


def _record_entry(facts: FactIndex, eid: str) -> Optional[EvidenceEntry]:
    src = facts.sources.get(eid)
    if src is None:
        return None
    lines = list(dict.fromkeys(f.excerpt for f in facts.facts if f.source_id == eid and f.excerpt))
    excerpt = "; ".join(lines[:_MAX_EXCERPT_LINES]) or src.source_type
    authority = src.authority if isinstance(src.authority, str) else src.authority.value
    label = _SOURCE_LABEL.get(src.source_type, src.source_type.replace("_", " "))
    return EvidenceEntry(
        source=f"{label} ({src.locator})" if src.locator else label,
        type="official_record" if authority == "official" else "third_party_record",
        excerpt=excerpt,
        as_of=src.retrieved_at.date() if src.retrieved_at else None,
    )


def _entry(eid: str, case: Optional[CaseInput], facts: Optional[FactIndex]) -> Optional[EvidenceEntry]:
    if eid.startswith("R"):
        return _record_entry(facts, eid) if facts else None
    return _case_entry(case, eid) if case is not None else None


def _sorted_ids(ids: set[str]) -> list[str]:
    return sorted(ids, key=lambda x: (x[0], int(x[1:])))


def build_evidence_index(ids: set[str], case: Optional[CaseInput],
                         facts: Optional[FactIndex]) -> tuple[dict[str, EvidenceEntry], list[str]]:
    index: dict[str, EvidenceEntry] = {}
    missing: list[str] = []
    for eid in _sorted_ids(ids):
        entry = _entry(eid, case, facts)
        if entry is None:
            missing.append(eid)
        else:
            index[eid] = entry
    return index, missing


@dataclass
class ChecksOutcome:
    check_result: Optional[CheckResult]                   # None when there is no memo
    evidence_index: dict[str, EvidenceEntry]
    lead_failures: list[LeadCheckFailure] = field(default_factory=list)
    report_index: dict[str, EvidenceEntry] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return (self.check_result is None or self.check_result.passed) and not self.lead_failures


def run_checks(memo: Optional[Memo], report: LeadReport, case: Optional[CaseInput],
               facts: Optional[FactIndex], plan: Optional[Plan], *,
               tin_redacted: bool = False) -> ChecksOutcome:
    memo_ids = memo.cited_ids() if memo else set()
    index, missing = build_evidence_index(memo_ids, case, facts)
    report_index, report_missing = build_evidence_index(report.cited_ids() - memo_ids, case, facts)
    failures: list[CheckFailure] = []
    lead: list[LeadCheckFailure] = []

    for eid in missing + report_missing:
        failures.append(CheckFailure(rule="CITED_ID_MUST_EXIST", evidence=[eid],
                                     detail=f"{eid} is not in the case or the saved sources"))

    dumped = (memo.model_dump_json() if memo else "") + report.model_dump_json()
    if tin_redacted or TIN_PATTERN.search(dumped):
        failures.append(CheckFailure(rule="NO_FULL_TAX_ID",
                                     detail="The LLM wrote something shaped like a full SSN or EIN; it was redacted"))

    flags = memo.flags if memo else []
    for f in flags:
        if f.status != "resolved":
            continue
        has_record = any(e[0] in "RK" for e in f.evidence)
        has_merchant = any(e[0] in "DBW" for e in f.evidence)
        if not (has_record and has_merchant):
            failures.append(CheckFailure(
                rule="CLEAR_NEEDS_TWO_SOURCES", flag=f.flag, evidence=list(f.evidence),
                detail=f"Resolved with {'records only' if has_record else 'merchant evidence only'} "
                       f"({', '.join(f.evidence) or 'no evidence'}). Cite both an R/K record and D/B/W evidence."))

    recommendation = memo.recommendation if memo else "insufficient_evidence"
    if facts is not None and case is not None:
        # The EIN belongs to an unrelated company.
        separate = facts.records("separate_entity")
        other = [r for r in tin_results(facts, case) if r.result == "Match"
                 and any(same_name(text(s.value), r.name_tested) for s in separate)]
        if other and recommendation != "escalate":
            failures.append(CheckFailure(rule="UNRELATED_EIN_MUST_ESCALATE", evidence=[other[-1].fact.source_id],
                                         detail=f"The EIN matches '{other[-1].name_tested}', a separate company "
                                                f"({other[-1].fact.source_id})"))
        # Not Issued TIN can't be cleared.
        results = tin_results(facts, case)
        if results and results[-1].result in ("Not Issued", "Not Found", "Never Issued") \
                and not any(r.result == "Match" for r in results):
            for f in flags:
                if f.status == "resolved" and FlagReason(f.flag) in _TIN_FLAGS:
                    failures.append(CheckFailure(rule="NOT_ISSUED_TIN_CANT_CLEAR", flag=f.flag,
                                                 evidence=[results[-1].fact.source_id],
                                                 detail="The IRS has not issued / has no record of the TIN"))
        # Blocking facts must escalate.
        blocking = [x for x in facts.records("kyc_result") if (text(x.value) or "").lower() == "fail"]
        blocking += [x for x in facts.evidence("tampered") if str(x.value).lower() in ("true", "yes", "1")]
        if blocking and recommendation != "escalate":
            lead.append(LeadCheckFailure(rule="HARD_FAIL_NOT_ESCALATED",
                                         detail="Blocking finding(s) " + ", ".join(b.source_id for b in blocking)))
    if any(c.status == "HARD_FAIL" for c in report.checks) and recommendation != "escalate" \
            and not any(f.rule == "HARD_FAIL_NOT_ESCALATED" for f in lead):
        lead.append(LeadCheckFailure(rule="HARD_FAIL_NOT_ESCALATED", detail="A check is HARD_FAIL"))

    if plan is not None and report.route == "reviewed":
        present = {c.check_id for c in report.checks}
        for cid in plan.required_ids():
            if cid not in present:
                lead.append(LeadCheckFailure(rule="REQUIRED_CHECK_MISSING", detail=f"{cid} is not in the report"))

    if case is not None and memo is not None:
        have = {d.type for d in case.documents}
        for req in memo.document_request:
            if req.type in have:
                lead.append(LeadCheckFailure(rule="DOCUMENT_ALREADY_IN_CASE",
                                             detail=f"The case already has a {req.type}"))

    for c in report.checks:
        if c.status == "EXPLAINED" and c.explanation_evidence and all(e.startswith("W") for e in c.explanation_evidence):
            lead.append(LeadCheckFailure(rule="WEBSITE_OVERRULES_RECORDS",
                                         detail=f"{c.check_id} is explained by the website alone"))

    result = CheckResult(passed=not failures, failures=failures) if memo is not None else None
    return ChecksOutcome(result, index, lead, report_index)


__all__ = ["ChecksOutcome", "build_evidence_index", "run_checks"]
