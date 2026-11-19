"""Scripted stand-ins for the Records and Evidence workers (BEAC-4 builds the real ones).

A script is a function from the WorkerInput to a list of ``Rec`` (one source each).
R IDs are numbered from the task's ``record_id_start``, as the real worker must do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from kyb_contracts.worker_io import WorkerInput, WorkerResult


@dataclass
class Rec:
    source_type: str
    findings: list[tuple[str, Any, str]]          # (field, value, excerpt)
    source_id: Optional[str] = None               # None -> next R ID
    authority: str = "official"
    subject: str = "business"
    confidence: str = "high"


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
            if sid not in {s["source_id"] for s in sources}:
                sources.append({"source_id": sid, "source_type": rec.source_type, "authority": rec.authority,
                                "locator": f"{rec.source_type}:{sid}", "retrieved_at": datetime(2026, 11, 17, 9, 0)})
            for fld, value, excerpt in rec.findings:
                n += 1
                findings.append({
                    "finding_id": f"f{n}", "subject": rec.subject, "field": fld, "value": value,
                    "source_id": sid, "extraction_method": "structured_record" if sid[0] in "RK" else "document_extraction",
                    "confidence": rec.confidence,
                    "citation": {"source_id": sid, "locator": f"{rec.source_type}:{sid}", "excerpt": excerpt},
                })
        return WorkerResult.model_validate({
            "task_id": task.task_id, "case_id": task.context.case_id, "worker": self.worker,
            "status": "completed", "findings": findings, "sources": sources, "hypothesis_signals": [],
            "tool_calls_used": len(recs), "answer": None,
        })


def kyc(kid: str = "K1", result: str = "pass", subject: str = "owner:P1") -> Rec:
    return Rec("owner_kyc", [("kyc_result", result, f"result: {result}")], source_id=kid,
               authority="third_party", subject=subject)


# ---------- case_1042 (docs/workflow-handoffs.md section 7) ----------

def records_1042(task: WorkerInput) -> list[Rec]:
    q = task.question
    if task.follow_up_of is None:
        return [
            Rec("sos_registry", [("legal_name", "NORTHLINE BICYCLE WORKS, L.L.C.", "NORTHLINE BICYCLE WORKS, L.L.C. | IL-0458821 | Active"),
                                 ("entity_status", "active", "status Active")]),
            Rec("county_dba", [("dba_name", "NORTHLINE CYCLES", "NORTHLINE CYCLES"),
                               ("dba_owner", "HARBOR LIGHT CYCLES LLC", "NORTHLINE CYCLES, filed 2024-01-15 by HARBOR LIGHT CYCLES LLC")]),
            Rec("irs_tin_match", [("tin_match", {"result": "Mismatch", "name_tested": "NORTHLINE BICYCLE WORKS LLC"},
                                   "EIN ending 4567 + NORTHLINE BICYCLE WORKS LLC: Mismatch")]),
            kyc(),
        ]
    if "TIN match again" in q:
        return [Rec("irs_tin_match", [("tin_match", {"result": "Match", "name_tested": "HARBOR LIGHT CYCLES LLC"},
                                       "EIN ending 4567 + HARBOR LIGHT CYCLES LLC: Match"),
                                      ("irs_name_control", "HARB", "name control HARB")])]
    if "former name" in q or "filing history" in q:
        return [Rec("sos_filing_history", [("former_name", {"old_name": "HARBOR LIGHT CYCLES LLC",
                                                            "new_name": "NORTHLINE BICYCLE WORKS LLC", "date": "2025-03-12"},
                                             "2025-03-12 Articles of Amendment: HARBOR LIGHT CYCLES LLC renamed NORTHLINE BICYCLE WORKS LLC")])]
    if "county-level DBA" in q:
        return [Rec("county_dba", [("dba_owner", "HARBOR LIGHT CYCLES LLC", "Cook County: NORTHLINE CYCLES, owner HARBOR LIGHT CYCLES LLC")])]
    return []


def evidence_1042(task: WorkerInput) -> list[Rec]:
    if task.follow_up_of is not None:
        return []
    return [
        Rec("irs_cp575", [("name", "HARBOR LIGHT CYCLES LLC", "IRS CP 575: HARBOR LIGHT CYCLES LLC"),
                          ("ein_last4", "4567", "EIN ending 4567"), ("date", "2023-06-08", "2023-06-08")],
            source_id="D1", authority="merchant", confidence="high"),
        Rec("bank_verification", [("account_holder", "NORTHLINE BICYCLE WORKS LLC DBA NORTHLINE CYCLES", "Account holder"),
                                  ("bank_status", "verified", "verified")], source_id="B1", authority="third_party"),
        Rec("website", [("legal_name", "Northline Bicycle Works LLC", "legal name"), ("dba", "Northline Cycles", "dba")],
            source_id="W1", authority="merchant", confidence="low"),
    ]


# ---------- case_3001: the EIN belongs to an unrelated company ----------

def records_3001(task: WorkerInput) -> list[Rec]:
    q = task.question
    if task.follow_up_of is None:
        return [
            Rec("sos_registry", [("legal_name", "MAPLE RIDGE COFFEE ROASTERS LLC", "MAPLE RIDGE COFFEE ROASTERS LLC"),
                                 ("entity_status", "active", "Active")]),
            Rec("irs_tin_match", [("tin_match", "3", "code 3: name/TIN combination does not match ('MAPLE RIDGE COFFEE ROASTERS LLC')")]),
            kyc(),
        ]
    if "TIN match again" in q:
        return [Rec("irs_tin_match", [("tin_match", {"result": "Match", "name_tested": "BLUE RIVER TOOLS INC"},
                                       "EIN ending 4321 + BLUE RIVER TOOLS INC: Match")])]
    if "former name" in q or "filing history" in q:
        return [Rec("sos_registry", [("separate_entity", {"name": "BLUE RIVER TOOLS INC", "state_entity_id": "OH-1100"},
                                      "BLUE RIVER TOOLS INC, OH-1100, an unrelated corporation")])]
    return []


def evidence_3001(task: WorkerInput) -> list[Rec]:
    if task.follow_up_of is not None:
        return []
    return [
        Rec("irs_147c", [("name", "BLUE RIVER TOOLS INC", "147C"), ("ein_last4", "4321", "4321"),
                         ("date", "2026-10-28", "2026-10-28")], source_id="D1", authority="merchant"),
        Rec("bank_verification", [("account_holder", "MAPLE RIDGE COFFEE ROASTERS LLC", "holder"),
                                  ("bank_status", "verified", "verified")], source_id="B1", authority="third_party"),
    ]


# ---------- case_2001: sole proprietor with a DBA ----------

def records_2001(task: WorkerInput) -> list[Rec]:
    q = task.question
    if task.follow_up_of is None:
        return [
            Rec("sos_registry", [("registry_found", False, "No NY entity named Bluebird Tailoring")]),
            Rec("county_dba", [("dba_name", "BLUEBIRD TAILORING", "BLUEBIRD TAILORING"),
                               ("dba_owner", "MAYA TORRES", "Kings County DBA BLUEBIRD TAILORING, registrant MAYA TORRES")]),
            Rec("irs_tin_match", [("tin_match", "3", "code 3: does not match ('Bluebird Tailoring')")]),
            kyc(),
        ]
    if "TIN match again" in q or "owner's name" in q:
        return [Rec("irs_tin_match", [("tin_match", "6", "code 6: matched on SSN with name 'Maya Torres'")],
                    subject="owner:P1")]
    return []


def evidence_2001(task: WorkerInput) -> list[Rec]:
    if task.follow_up_of is not None:
        return []
    return [
        Rec("dba_certificate", [("dba", "BLUEBIRD TAILORING", "dba"), ("registrant", "MAYA TORRES", "registrant"),
                                ("filed", "2026-09-02", "filed")], source_id="D1", authority="merchant"),
        Rec("w9", [("legal_name", "Maya Torres", "w9 name"), ("business_name", "Bluebird Tailoring", "w9 business"),
                   ("tin_type", "SSN", "SSN"), ("tin_last4", "3456", "3456")], source_id="D2", authority="merchant"),
        Rec("bank_verification", [("account_holder", "MAYA TORRES DBA BLUEBIRD TAILORING", "holder"),
                                  ("bank_status", "verified", "verified")], source_id="B1", authority="third_party"),
    ]
