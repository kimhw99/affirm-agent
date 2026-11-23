"""Step 3: the Lead's plan, a list of claims to verify.

Each check says what the records side should show, what the evidence side should
show, and what counts as a pass. The plan starts from a fixed template per edge case.
The LLM may *add* case-specific checks (``ADD.`` prefix, optional), but it can never
remove or weaken a required template check: ``build_plan`` re-applies the templates
after merging.

``expected_absence`` is decided here, up front. For EC-4, "not found in the registry"
is often normal for a new business or a sole proprietor, and the plan must say so
before reconcile sees the result, or a normal new business looks like a conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

from app.graph.facts import worker_fields
from app.graph.orchestrator import EVIDENCE_TASKS, RECORDS_TASKS, RoutingDecision
from kyb_contracts.case_input import CaseInput, FlagReason
from kyb_contracts.worker_io import (
    BankVerificationRef,
    CaseContext,
    DocumentRef,
    Hypothesis,
    OwnerKycRef,
    WebSnapshotRef,
    WorkerInput,
)

# A registry can take weeks to show a new filing; an EIN can take weeks to reach IRS systems.
REGISTRY_LAG_DAYS = 90
EIN_LAG_DAYS = 60

EC1 = (FlagReason.NAME_SIMILAR_MATCH, FlagReason.NAME_UNVERIFIED, FlagReason.DBA_UNVERIFIED)
EC2 = (FlagReason.TIN_MISMATCH, FlagReason.TIN_NOT_FOUND)
EC4 = (FlagReason.TIN_NOT_ISSUED, FlagReason.ENTITY_NOT_FOUND, FlagReason.NO_COMMERCIAL_RECORD)


class PlannedCheck(BaseModel):
    check_id: str
    edge_case: Literal["EC-1", "EC-2", "EC-4"]
    description: str
    origin: Literal["template", "lead_added"] = "template"
    required: bool = True
    expected_absence: bool = False
    records_side: str
    evidence_side: str
    pass_rule: str
    records_focus: list[str] = Field(default_factory=list)
    evidence_focus: list[str] = Field(default_factory=list)
    flags: list[FlagReason] = Field(default_factory=list)


class Plan(BaseModel):
    checks: list[PlannedCheck]
    hypotheses: list[Hypothesis]
    records_question: str
    evidence_question: str

    def check(self, check_id: str) -> PlannedCheck:
        for c in self.checks:
            if c.check_id == check_id:
                return c
        raise KeyError(check_id)

    def checks_for_flag(self, flag: FlagReason) -> list[str]:
        return [c.check_id for c in self.checks if flag in c.flags]

    def required_ids(self) -> list[str]:
        return [c.check_id for c in self.checks if c.required]


# ---------- Templates ----------

@dataclass(frozen=True)
class CheckTemplate:
    check_id: str
    edge_case: Literal["EC-1", "EC-2", "EC-4"]
    description: str
    flags: tuple[FlagReason, ...]
    records_side: str
    evidence_side: str
    pass_rule: str
    records_focus: tuple[str, ...]
    evidence_focus: tuple[str, ...]
    applies: Callable[[CaseInput], bool] = lambda case: True
    expected_absence: Callable[[CaseInput, date], bool] = lambda case, today: False


def _is_sole_prop(case: CaseInput) -> bool:
    return case.application.entity_type == "SOLE_PROPRIETORSHIP"


def _has_dba(case: CaseInput) -> bool:
    return bool(case.application.dba)


def _newly_formed(case: CaseInput, today: date) -> bool:
    """The plan expects no registry entry: a sole proprietor, or a recent formation."""
    if _is_sole_prop(case):
        return True
    formed = case.application.formation_date
    if formed is not None:
        return (today - formed).days <= REGISTRY_LAG_DAYS
    # No date given, but formation papers were uploaded: reconcile checks their date.
    return any(d.type == "articles" for d in case.documents)


TEMPLATES: tuple[CheckTemplate, ...] = (
    # ----- EC-1: legal name vs DBA, suffix and punctuation variants -----
    CheckTemplate(
        "EC1.entity_active", "EC-1", "The legal entity exists and is active", EC1,
        "State registry lookup, by name and by entity number",
        "Name on the business license or formation documents",
        "The registry shows an active entity whose normalized name matches the documents",
        ("legal_name", "entity_status", "state_entity_id"),
        ("legal_name", "holder", "status"),
        applies=lambda case: not _is_sole_prop(case),
    ),
    CheckTemplate(
        "EC1.applied_name", "EC-1",
        "The applied legal name is the registered name at the submitted entity number", EC1,
        "Registry entity at the submitted entity number: legal name, officers, former names; "
        "DBA filings of that entity",
        "— (the application itself)",
        "The names match after normalization, or a former name or a DBA the entity filed links them; "
        "an entity with none of the applicant's owners as officers is another company",
        ("legal_name", "state_entity_id", "officer", "former_name", "dba_name", "dba_owner",
         "dba_owner_entity_id"),
        (),
        applies=lambda case: not _is_sole_prop(case) and bool(case.application.state_entity_id),
    ),
    CheckTemplate(
        "EC1.dba_owner", "EC-1", "The DBA belongs to the legal entity",
        (FlagReason.DBA_UNVERIFIED, FlagReason.NAME_UNVERIFIED),
        "DBA / assumed-name filing that lists the legal entity as owner",
        "Website footer, invoices, the 'Legal Name DBA X' line on the bank account",
        "The DBA filing's owner is the legal entity, and the evidence ties the DBA to it",
        ("dba_name", "dba_owner"),
        ("dba", "registrant", "account_holder", "website_legal_name"),
        applies=lambda case: _has_dba(case) and not _is_sole_prop(case),
    ),
    CheckTemplate(
        "EC1.name_normalized", "EC-1", "The names match after normalization",
        (FlagReason.NAME_SIMILAR_MATCH, FlagReason.NAME_UNVERIFIED),
        "Normalized registry name",
        "Normalized names from the documents (suffixes, & vs 'and', punctuation removed)",
        "Every document name equals the registry name after normalization",
        ("legal_name", "former_name"),
        ("legal_name", "business_name", "holder"),
        applies=lambda case: not _is_sole_prop(case),
    ),
    CheckTemplate(
        "EC1.bank_account_holder", "EC-1",
        "The bank account holder is the legal name or 'legal name DBA name'", EC1,
        "—", "Account name on the verified bank account",
        "The account holder's legal part matches the legal name (and its DBA part the DBA)",
        ("legal_name",),
        ("account_holder", "bank_status"),
    ),
    # ----- EC-2: EIN/TIN mismatch and IRS name control -----
    CheckTemplate(
        "EC2.tin_match", "EC-2", "The IRS accepts the submitted name and TIN pair", EC2,
        "IRS TIN match for the submitted name and TIN",
        "Name on the IRS EIN letter (CP 575 / 147C)",
        "TIN match returns Match for the submitted name, or a cited reason explains the mismatch",
        ("tin_match", "irs_name_control"),
        ("name", "ein_last4"),
    ),
    CheckTemplate(
        "EC2.name_control", "EC-2", "The IRS name control fits the EIN letter", EC2,
        "The name control the IRS likely has on file",
        "Name control on (or computed from) the EIN letter",
        "The two name controls are equal",
        ("irs_name_control", "former_name"),
        ("name_control", "name", "legal_name"),
    ),
    CheckTemplate(
        "EC2.ein_letter_matches", "EC-2", "The EIN on the letter is the submitted EIN", EC2,
        "The submitted TIN (last 4)", "EIN last 4 printed on the letter",
        "Exact match; any difference is a hard fail",
        ("tin_match",),
        ("ein_last4",),
        applies=lambda case: case.application.tin_type != "SSN",
    ),
    CheckTemplate(
        "EC2.tin_type_fits", "EC-2", "The TIN type fits the entity type", EC2,
        "Entity type (registry or application)", "TIN type on the W-9 or application",
        "An EIN for any entity; an SSN only for a sole proprietor or single-member LLC",
        ("entity_type",),
        ("tin_type",),
    ),
    # ----- EC-4: new businesses and sole proprietors -----
    CheckTemplate(
        "EC4.registry_presence", "EC-4",
        "The business is registered, or its absence is expected (new filing or sole proprietor)",
        (FlagReason.ENTITY_NOT_FOUND, FlagReason.NO_COMMERCIAL_RECORD, FlagReason.TIN_NOT_ISSUED),
        "Registry lookup (a 'pending' status is a filed formation not yet approved) and filing history",
        "Articles or certificate of formation with a recent filing stamp",
        "Found and active; or pending or not found and the plan expected it (recent formation papers, "
        "or a sole proprietor)",
        ("registry_found", "entity_status", "formation_date"),
        ("formation_date", "date", "legal_name"),
        expected_absence=_newly_formed,
    ),
    CheckTemplate(
        "EC4.ein_age", "EC-4", "The EIN is new and consistent with the formation date",
        (FlagReason.TIN_NOT_ISSUED,),
        "IRS TIN match result (a fresh EIN may not be in IRS systems yet)",
        "CP 575 issue date",
        "TIN match succeeds, or the CP 575 is recent enough to explain an IRS lag",
        ("tin_match",),
        ("date", "ein_last4", "name"),
        applies=lambda case: case.application.tin_type != "SSN",
    ),
    CheckTemplate(
        "EC4.sole_prop_owner", "EC-4", "The sole proprietor is the owner the TIN belongs to",
        (FlagReason.ENTITY_NOT_FOUND, FlagReason.NO_COMMERCIAL_RECORD, FlagReason.TIN_NOT_ISSUED),
        "Owner KYC; TIN match against the owner's name",
        "W-9 / bank account in the owner's name",
        "KYC passes and the TIN matches the owner (SSN or an EIN issued to the owner)",
        ("kyc_result", "tin_match"),
        ("legal_name", "account_holder", "tin_type"),
        applies=_is_sole_prop,
    ),
    CheckTemplate(
        "EC4.sole_prop_dba", "EC-4", "The trade name is filed as a DBA of the owner",
        (FlagReason.ENTITY_NOT_FOUND, FlagReason.DBA_UNVERIFIED, FlagReason.NO_COMMERCIAL_RECORD),
        "County DBA filing naming the owner", "DBA certificate / 'Owner DBA X' bank line",
        "The DBA filing's registrant is the owner",
        ("dba_name", "dba_owner"),
        ("dba", "registrant", "account_holder"),
        applies=lambda case: _is_sole_prop(case) and _has_dba(case),
    ),
    CheckTemplate(
        "EC4.operating", "EC-4", "The business is really operating",
        (FlagReason.ENTITY_NOT_FOUND, FlagReason.NO_COMMERCIAL_RECORD),
        "—", "Verified bank account, active license, website snapshot, DBA certificate",
        "A verified bank account, plus an active license, a website naming the business or a filed DBA certificate",
        (),
        ("bank_status", "license_status", "website_legal_name"),
    ),
)


def template_checks(case: CaseInput, open_flags: list[FlagReason], today: date) -> list[PlannedCheck]:
    """The fixed, required checks for the case's open flags. Pure code; tested directly."""
    flags = set(open_flags)
    out: list[PlannedCheck] = []
    for t in TEMPLATES:
        hit = [f for f in t.flags if f in flags]
        if not hit or not t.applies(case):
            continue
        out.append(PlannedCheck(
            check_id=t.check_id,
            edge_case=t.edge_case,
            description=t.description,
            required=True,
            expected_absence=t.expected_absence(case, today),
            records_side=t.records_side,
            evidence_side=t.evidence_side,
            pass_rule=t.pass_rule,
            records_focus=list(t.records_focus),
            evidence_focus=list(t.evidence_focus),
            flags=hit,
        ))
    return out


# ---------- What the LLM may add ----------

class AddedCheck(BaseModel):
    slug: str = Field(description="snake_case name, e.g. second_dba")
    edge_case: Literal["EC-1", "EC-2", "EC-4"]
    description: str
    records_focus: list[str] = Field(default_factory=list)
    evidence_focus: list[str] = Field(default_factory=list)
    rationale: str = ""


class PlanExtras(BaseModel):
    """What the LLM contributes to a plan. It can't remove or change template checks."""

    added_checks: list[AddedCheck] = Field(default_factory=list, max_length=3)
    hypotheses: list[str] = Field(default_factory=list, max_length=5)
    records_question: Optional[str] = None
    evidence_question: Optional[str] = None


DEFAULT_HYPOTHESES: dict[str, list[str]] = {
    "EC-1": ["Formatting-only name difference (suffix, punctuation, 'The')",
             "Trade name (DBA) of the legal entity",
             "The entity changed its legal name"],
    "EC-2": ["The IRS has a former legal name on file",
             "Sole proprietor or single-member LLC whose TIN is tied to the owner",
             "The EIN belongs to a different entity"],
    "EC-4": ["Newly formed; the registry or IRS hasn't caught up yet",
             "Sole proprietor with no registry entry",
             "The business doesn't exist or isn't operating"],
}

_SLUG = re.compile(r"[^a-z0-9_]+")


def build_plan(case: CaseInput, decision: RoutingDecision, today: date,
               extras: Optional[PlanExtras] = None) -> Plan:
    checks = template_checks(case, decision.open_flags, today)
    template_ids = {c.check_id for c in checks}

    if extras:
        for added in extras.added_checks:
            slug = _SLUG.sub("_", added.slug.lower()).strip("_")[:40] or "extra"
            cid = f"ADD.{slug}"
            if cid in template_ids or any(c.check_id == cid for c in checks):
                continue
            checks.append(PlannedCheck(
                check_id=cid,
                edge_case=added.edge_case,
                description=added.description,
                origin="lead_added",
                required=False,             # the LLM can add checks, never new blockers
                records_side=", ".join(added.records_focus) or "—",
                evidence_side=", ".join(added.evidence_focus) or "—",
                pass_rule=added.rationale or "Both sides agree after normalization",
                records_focus=added.records_focus,
                evidence_focus=added.evidence_focus,
                flags=[],
            ))

    # Safety net: every template check is still present and still required.
    assert template_ids <= {c.check_id for c in checks if c.required}

    hyp_texts: list[str] = []
    for ec in decision.edge_cases:
        hyp_texts += DEFAULT_HYPOTHESES.get(ec, [])
    if extras:
        hyp_texts += extras.hypotheses
    hyp_texts = list(dict.fromkeys(hyp_texts))
    hypotheses = [Hypothesis(hypothesis_id=f"h{i}", text=t) for i, t in enumerate(hyp_texts, 1)]

    return Plan(
        checks=checks,
        hypotheses=hypotheses,
        records_question=(extras and extras.records_question) or _default_question(checks, "records", decision),
        evidence_question=(extras and extras.evidence_question) or _default_question(checks, "evidence", decision),
    )


def _default_question(checks: list[PlannedCheck], worker: str, decision: RoutingDecision) -> str:
    tasks = decision.records_tasks if worker == "records" else decision.evidence_tasks
    table = RECORDS_TASKS if worker == "records" else EVIDENCE_TASKS
    lines = []
    for c in checks:
        side = c.records_side if worker == "records" else c.evidence_side
        if side and side != "—":
            lines.append(f"[{c.check_id}] {c.description}: what do {side.lower()} show?")
    if tasks:
        lines.append("Lookups to run: " + "; ".join(table[t] for t in tasks) + ".")
    return " ".join(lines)


# ---------- WorkerInput construction ----------

def case_context(case: CaseInput) -> CaseContext:
    """Submitted facts and evidence keys only: no extracted document or website values."""
    return CaseContext(
        case_id=case.case_id,
        flag_reasons=case.flag_reasons,
        application=case.application,
        checks=case.checks,
        documents=[DocumentRef(id=d.id, type=d.type) for d in case.documents],
        bank_verification=(BankVerificationRef(id=case.bank_verification.id)
                           if case.bank_verification else None),
        web_snapshot=[WebSnapshotRef(id=w.id, url=str(w.url), captured_at=w.captured_at)
                      for w in case.web_snapshot],
        owner_kyc=[OwnerKycRef(id=k.id, owner_ref=k.owner_ref) for k in case.owner_kyc],
    )


def tool_budget(n_tasks: int, n_checks: int) -> int:
    return max(3, min(20, 2 + n_tasks + n_checks))


def initial_tasks(case: CaseInput, decision: RoutingDecision, plan: Plan) -> list[WorkerInput]:
    """One WorkerInput per worker that has lookups to do (the routing table decides)."""
    ctx = case_context(case)
    out: list[WorkerInput] = []
    for worker, tasks, question in (
        ("records", decision.records_tasks, plan.records_question),
        ("evidence", decision.evidence_tasks, plan.evidence_question),
    ):
        if not tasks:
            continue
        focus: list[str] = []
        for c in plan.checks:
            focus += c.records_focus if worker == "records" else c.evidence_focus
        out.append(WorkerInput(
            task_id=f"{case.case_id}-{worker}-1",
            worker=worker,
            question=question,
            focus_fields=worker_fields(worker, focus) or list(tasks),
            hypotheses=plan.hypotheses,
            context=ctx,
            max_tool_calls=tool_budget(len(tasks), len(plan.checks)),
            record_id_start=1,
        ))
    return out


__all__ = [
    "AddedCheck",
    "CheckTemplate",
    "DEFAULT_HYPOTHESES",
    "EIN_LAG_DAYS",
    "Plan",
    "PlanExtras",
    "PlannedCheck",
    "REGISTRY_LAG_DAYS",
    "TEMPLATES",
    "build_plan",
    "case_context",
    "initial_tasks",
    "template_checks",
    "tool_budget",
]
