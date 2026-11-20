"""Scripted stand-ins for the Records and Evidence workers.

They return what the BEAC-7 workers' tools write (``app/tools``): the same finding
``field`` names and value shapes, R sources with the same ``source_type`` and
locators, and excerpts as ``key: value`` lines. So these tests show how the Lead reads
the real workers, without a chat model. (``tests/test_real_workers.py`` runs the real
workers, when they are installed.)

A script is a function from the WorkerInput to a list of ``Rec`` (one source each).
R IDs are numbered from the task's ``record_id_start``, as the real worker does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from kyb_contracts.worker_io import WorkerInput, WorkerResult

from app.shared.name_norm import irs_name_control

Finding = tuple  # (field, value, excerpt) or (field, value, excerpt, note)


@dataclass
class Rec:
    source_type: str
    findings: list[Finding]
    source_id: Optional[str] = None               # None -> next R ID
    authority: str = "official"
    subject: str = "business"
    confidence: str = "high"
    locator: Optional[str] = None


Script = Callable[[WorkerInput], list[Rec]]


@dataclass
class ScriptedWorker:
    worker: str
    script: Script
    calls: list[WorkerInput] = field(default_factory=list)

    def run(self, task: WorkerInput) -> WorkerResult:
        assert task.worker == self.worker
        self.calls.append(task)
        recs = self.script(task)
        next_r = task.record_id_start
        sources, findings = [], []
        n = 0
        for rec in recs:
            sid = rec.source_id
            if sid is None:
                sid = f"R{next_r}"
                next_r += 1
            locator = rec.locator or f"{rec.source_type}:{sid}"
            if sid not in {s["source_id"] for s in sources}:
                sources.append({"source_id": sid, "source_type": rec.source_type, "authority": rec.authority,
                                "locator": locator, "retrieved_at": datetime(2026, 11, 17, 9, 0)})
            for fld, value, excerpt, *note in rec.findings:
                n += 1
                findings.append({
                    "finding_id": f"f_{task.task_id}_{n}", "subject": rec.subject, "field": fld, "value": value,
                    "source_id": sid,
                    "extraction_method": "structured_record" if sid[0] in "RKB" else "document_extraction",
                    "confidence": rec.confidence,
                    "citation": {"source_id": sid, "locator": locator, "excerpt": excerpt},
                    "note": note[0] if note else None,
                })
        return WorkerResult.model_validate({
            "task_id": task.task_id, "case_id": task.context.case_id, "worker": self.worker,
            "status": "completed", "findings": findings, "sources": sources, "hypothesis_signals": [],
            "tool_calls_used": len(recs), "answer": None,
        })


# ---------- What each BEAC-7 tool writes ----------

def registry_entity(entity_id: str, legal_name: str, status: str = "active", formed: str = "2021-08-16",
                    former: tuple[str, ...] = (), state: str = "IL") -> Rec:
    """get_registry_entity / search_registry: one entity."""
    f = [("legal_name", legal_name, f"legal_name: {legal_name}"),
         ("state_entity_id", entity_id, f"state_entity_id: {entity_id}"),
         ("entity_status", status, f"entity_status: {status}"),
         ("formation_date", formed, f"formation_date: {formed}")]
    if former:
        f.append(("former_names", list(former), "former_names: " + "; ".join(former)))
    return Rec("sos_registry", f, locator=f"{state} SOS {entity_id}")


def registry_none(name: str, state: str = "NY") -> Rec:
    """search_registry with no results."""
    return Rec("sos_registry_search", [("registry_match", "none", "results: 0", f"No {state} registry entity for name={name}")],
               locator=f"{state} SOS search (name={name})")


def name_change(entity_id: str, old: str, new: str, when: str) -> Rec:
    """get_filing_history with one name change."""
    line = f"event: name_change {when}: {old} -> {new} [{entity_id}-2]"
    return Rec("sos_filing_history", [
        ("filing_event", {"event_id": f"{entity_id}-2", "event_type": "name_change", "effective_date": when,
                          "old_value": old, "new_value": new}, line),
        ("former_name", old, line, f"name used until {when}"),
    ], locator=f"IL SOS {entity_id} filings")


def dba_filing(filing_id: str, dba: str, registrant: str, registrant_entity_id: Optional[str] = None,
               county: Optional[str] = None, status: str = "active") -> Rec:
    """search_dba: one matching filing (status as in world.DbaFiling)."""
    f = [("dba", dba, f"dba: {dba}"), ("registrant", registrant, f"registrant: {registrant}"),
         ("dba_status", status, f"status: {status}"),
         ("dba_expired", status == "expired", f"status: {status}")]
    if registrant_entity_id:
        f.append(("registrant_state_entity_id", registrant_entity_id,
                  f"registrant_state_entity_id: {registrant_entity_id}"))
    return Rec("county_dba" if county else "state_dba", f, locator=filing_id)


def tin_match(case_id: str, name: str, result: str, subject: str = "business", individual: bool = False) -> Rec:
    """check_tin_match: the result for one tested name. The IRS name is never shown."""
    nc = irs_name_control(name, is_individual=individual)
    return Rec("irs_tin_match", [
        ("tin_match_result", result, f"result: {result}", f"the IRS name on file vs {name!r}"),
        ("name_control_tested", nc, f"name_control_tested: {nc}", f"name tested: {name}"),
    ], subject=subject, locator=f"tin-record:{case_id} name={name}")


def kyc(kid: str = "K1", result: str = "pass", subject: str = "owner:P1") -> Rec:
    return Rec("owner_kyc", [("kyc_result", result, f"result: {result}")], source_id=kid,
               authority="third_party", subject=subject)


def document(doc_id: str, doc_type: str, confidence: str = "high", **extracted: Any) -> Rec:
    """get_document: each extracted field under its own name."""
    return Rec(doc_type, [(k, v, f"extracted.{k}: {v}") for k, v in extracted.items()], source_id=doc_id,
               authority="merchant", confidence=confidence)


def bank(holder: str, status: str = "verified") -> Rec:
    return Rec("bank_verification", [("account_holder", holder, f"account_holder: {holder}"),
                                     ("bank_status", status, f"status: {status}")],
               source_id="B1", authority="third_party")


def website(legal_name: Optional[str] = None, dba: Optional[str] = None, suspicious: bool = False) -> Rec:
    f = []
    if legal_name:
        f.append(("website_legal_name", legal_name, f"extracted.legal_name: {legal_name}"))
    if dba:
        f.append(("website_dba", dba, f"extracted.dba: {dba}"))
    f.append(("injection_suspected", suspicious, "url: https://site.example"))
    return Rec("website", f, source_id="W1", authority="merchant", confidence="low")


# ---------- case_1042 (docs/workflow-handoffs.md section 7) ----------
# The entity record doesn't list former names and the DBA filing has no entity number,
# so the former name only turns up in the filing history, on a follow-up.

def records_1042(task: WorkerInput) -> list[Rec]:
    q = task.question
    if task.follow_up_of is None:
        return [
            registry_entity("IL-0458821", "NORTHLINE BICYCLE WORKS, L.L.C.", formed="2023-05-30"),
            dba_filing("IL-AN-2024-0115", "NORTHLINE CYCLES", "HARBOR LIGHT CYCLES LLC"),
            tin_match("case_1042", "Northline Bicycle Works LLC", "Mismatch"),
            kyc(),
        ]
    if "TIN match again" in q:
        return [tin_match("case_1042", "HARBOR LIGHT CYCLES LLC", "Match")]
    if "former name" in q or "filing history" in q:
        return [name_change("IL-0458821", "HARBOR LIGHT CYCLES LLC", "NORTHLINE BICYCLE WORKS, L.L.C.", "2025-03-12")]
    if "county-level DBA" in q:
        return [dba_filing("IL-COOK-2024-77", "NORTHLINE CYCLES", "HARBOR LIGHT CYCLES LLC", county="Cook")]
    return []


def evidence_1042(task: WorkerInput) -> list[Rec]:
    if task.follow_up_of is not None:
        return []
    return [
        document("D1", "irs_cp575", name="HARBOR LIGHT CYCLES LLC", ein_last4="4567", date="2023-06-08"),
        bank("NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES"),
        website(legal_name="Northline Bicycle Works LLC", dba="Northline Cycles"),
    ]


# ---------- case_3001: the EIN belongs to an unrelated company ----------

def records_3001(task: WorkerInput) -> list[Rec]:
    q = task.question
    if task.follow_up_of is None:
        return [
            registry_entity("OH-5582114", "MAPLE RIDGE COFFEE ROASTERS LLC", state="OH"),
            tin_match("case_3001", "Maple Ridge Coffee Roasters LLC", "Mismatch"),
            kyc(),
        ]
    if "TIN match again" in q:
        return [tin_match("case_3001", "BLUE RIVER TOOLS INC", "Match")]
    if "former name" in q or "filing history" in q:
        # The letter's name is a different company in the registry, not a former name.
        return [registry_entity("OH-1100", "BLUE RIVER TOOLS INC", formed="2009-02-01", state="OH")]
    return []


def evidence_3001(task: WorkerInput) -> list[Rec]:
    if task.follow_up_of is not None:
        return []
    return [
        document("D1", "irs_147c", name="BLUE RIVER TOOLS INC", ein_last4="4321", date="2026-10-28"),
        bank("MAPLE RIDGE COFFEE ROASTERS LLC"),
    ]


# ---------- case_2001: sole proprietor with a DBA ----------

def records_2001(task: WorkerInput) -> list[Rec]:
    q = task.question
    if task.follow_up_of is None:
        return [
            registry_none("Bluebird Tailoring"),
            dba_filing("NY-KINGS-2026-0912", "BLUEBIRD TAILORING", "MAYA TORRES", county="Kings"),
            tin_match("case_2001", "Bluebird Tailoring", "Mismatch", individual=True),
            kyc(),
        ]
    if "TIN match again" in q or "owner's name" in q:
        return [tin_match("case_2001", "Maya Torres", "Match", subject="owner:P1", individual=True)]
    return []


def evidence_2001(task: WorkerInput) -> list[Rec]:
    if task.follow_up_of is not None:
        return []
    return [
        document("D1", "dba_certificate", dba="BLUEBIRD TAILORING", registrant="MAYA TORRES", filed="2026-09-02"),
        document("D2", "w9", legal_name="Maya Torres", business_name="Bluebird Tailoring", tin_type="SSN",
                 tin_last4="3456"),
        bank("MAYA TORRES DBA BLUEBIRD TAILORING"),
    ]
