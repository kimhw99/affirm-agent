"""Evidence worker tools: documents, bank verification, website snapshots (endpoints 8-10).

Everything here is the merchant's claim (documents, website) or verified by the bank
(bank verification). Values are stored fields the existing system already extracted;
nothing is parsed here. Website values are always low confidence, and any value that
reads like an instruction to an AI becomes an ``injection_suspected`` finding. It is
reported, never followed.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import Field

from kyb_contracts.case_input import DOCUMENT_FIELDS, Document
from kyb_contracts.worker_io import Authority, ExtractionMethod

from app.shared.name_norm import irs_name_control, split_dba

from ._common import (
    Record,
    ToolArgs,
    ToolContext,
    ToolInputError,
    ToolOutput,
    ToolSpec,
    addresses_match,
    confidence_from_score,
    output_from,
    parse_date,
)

DOC = ExtractionMethod.DOCUMENT_EXTRACTION
PAGE = ExtractionMethod.PAGE_TEXT
STRUCTURED = ExtractionMethod.STRUCTURED_RECORD
DERIVED = ExtractionMethod.DERIVED

NAME_FIELDS = ("name", "legal_name", "business_name", "dba", "registrant", "holder", "old_name", "new_name")
DATE_FIELDS = ("date", "issued", "filed")      # the document's own date, for its age
STALE_DAYS = {"good_standing": 90, "irs_147c": 365}
LOW_EXTRACTION = 0.7

# Field order per document type, so findings come out in a stable order.
FIELD_ORDER = ("name", "legal_name", "business_name", "old_name", "new_name", "dba", "registrant", "holder",
               "ein_last4", "tin_type", "tin_last4", "name_control", "state", "state_entity_id",
               "formation_date", "jurisdiction", "status", "authority", "address", "date", "issued",
               "filed", "expires")

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|previous|prior|above|rules|prompt)\b",
        r"\b(system|assistant|developer)\s*(prompt|message)?\s*:",
        r"\byou\s+are\s+(now\s+)?(an?|the)\s+\w+",
        r"\b(recommend|recommendation)\b.{0,30}\b(approv\w*|den(y|ial)|declin\w*)\b",
        r"\b(approve|deny)\s+(this|the)\s+(merchant|application|case|business)\b",
        r"\breport\s+that\b",
        r"\b(as\s+an?\s+)?(AI|LLM|language\s+model|assistant|agent)\b.{0,30}\b(must|should|will)\b",
        r"</?\s*(system|instructions?|untrusted\w*|prompt)\s*>",
        r"\bdo\s+not\s+(flag|mention|report)\b",
    )
]


def injection_like(value: str) -> bool:
    return any(p.search(value) for p in INJECTION_PATTERNS)


def _allowed(ctx: ToolContext, kind: str, wanted: Optional[str], available: list[str]) -> str:
    if wanted is None:
        if len(available) == 1:
            return available[0]
        raise ToolInputError(f"{kind} is required; this case has {available or 'none'}")
    if wanted not in available:
        raise ToolInputError(f"{wanted} is not a {kind} of this case for the {ctx.worker} worker; "
                             f"available: {available or 'none'}")
    return wanted


# --------------------------------------------------------------------------
# Documents (endpoint 8)
# --------------------------------------------------------------------------

class GetDocumentArgs(ToolArgs):
    doc_id: str = Field(pattern=r"^D\d+$", description="A document ID from the task, e.g. D1.")


def get_document(ctx: ToolContext, args: GetDocumentArgs) -> ToolOutput:
    doc_id = _allowed(ctx, "doc_id", args.doc_id, [d.id for d in ctx.task.context.documents])
    call_key = f"doc|{doc_id}"
    if (hit := ctx.cached(call_key, "get_document")):
        return hit
    doc = ctx.client.get_document(ctx.case_id, doc_id)
    if doc is None:
        raise ToolInputError(f"The case service has no document {doc_id} for this case.")

    fields = [(k, doc.extracted[k]) for k in FIELD_ORDER
              if k in doc.extracted and k in DOCUMENT_FIELDS[doc.type]]
    record = Record(f"Document {doc.id} ({doc.type}), fields extracted by the existing KYB system")
    record.add("type", doc.type)
    l_conf = record.add("confidence", doc.confidence)
    lines = {k: record.add(f"extracted.{k}", v) for k, v in fields}
    source = ctx.add_source(call_key, source_type=doc.type, authority=Authority.MERCHANT,
                            locator=f"{ctx.case_id}/{doc.id}", rendered=record.text, source_id=doc.id)
    conf = confidence_from_score(doc.confidence)
    add = ctx.add_finding
    findings = [add(source, k, v, lines[k], method=DOC, confidence=conf) for k, v in fields]
    findings += _document_derived(ctx, doc, source, lines, l_conf, conf)
    return ctx.remember(call_key, output_from(ctx, "get_document", [source], findings))


def _document_derived(ctx: ToolContext, doc: Document, source, lines: dict[str, str], l_conf: str,
                      conf: str) -> list:
    app, add, found = ctx.app, ctx.add_finding, []
    x = doc.extracted
    sole_prop = app.entity_type == "SOLE_PROPRIETORSHIP"

    for key in NAME_FIELDS:
        if key in lines and not injection_like(x[key]):
            include_owners = key in {"registrant", "holder"} or doc.type == "w9" or sole_prop
            ctx.name_hints(f"{doc.id}.{key}", x[key], include_owners=include_owners)

    for key in ("ein_last4", "tin_last4"):
        if key in lines and app.tin_last4:
            same = x[key] == app.tin_last4
            found.append(add(source, "tin_last4_matches_application", same, lines[key], method=DERIVED,
                             confidence=conf, note=f"{doc.id}.{key} {x[key]} vs application {app.tin_last4}"))
    if "tin_type" in lines and app.tin_type:
        found.append(add(source, "tin_type_matches_application", x["tin_type"].upper() == app.tin_type,
                         lines["tin_type"], method=DERIVED, confidence=conf))

    if doc.type in {"irs_cp575", "irs_147c"} and ("name_control" in lines or "name" in lines):
        is_individual = app.tin_type == "SSN"
        letter_nc = x.get("name_control") or irs_name_control(x["name"], is_individual=is_individual)
        applied_nc = irs_name_control(app.legal_name, is_individual=is_individual)
        line = lines.get("name_control") or lines["name"]
        found.append(add(source, "name_control_matches_legal_name", letter_nc.upper() == applied_nc, line,
                         method=DERIVED, confidence=conf,
                         note=f"letter name control {letter_nc.upper()} vs applied legal name {applied_nc}"))

    if "state_entity_id" in lines and app.state_entity_id:
        found.append(add(source, "state_entity_id_matches_application",
                         x["state_entity_id"].strip().upper() == app.state_entity_id.upper(),
                         lines["state_entity_id"], method=DERIVED, confidence=conf))
    if "formation_date" in lines and app.formation_date:
        found.append(add(source, "formation_date_matches_application",
                         parse_date(x["formation_date"]) == app.formation_date,
                         lines["formation_date"], method=DERIVED, confidence=conf))

    for key in DATE_FIELDS:
        when = parse_date(x.get(key))
        if key in lines and when:
            age = (ctx.today - when).days
            note = f"{doc.id}.{key} {when.isoformat()} is {age} day(s) before {ctx.today}"
            limit = STALE_DAYS.get(doc.type)
            if limit and age > limit:
                note += f"; older than {limit} days, so stale for a {doc.type}"
            found.append(add(source, "document_age_days", age, lines[key], method=DERIVED, confidence=conf,
                             note=note))
            break
    expires = parse_date(x.get("expires"))
    if "expires" in lines and expires:
        found.append(add(source, "document_expired", expires < ctx.today, lines["expires"], method=DERIVED,
                         confidence=conf))

    if doc.confidence < LOW_EXTRACTION:
        found.append(add(source, "low_extraction_confidence", True, l_conf, method=DERIVED,
                         confidence="low",
                         note=f"extraction confidence {doc.confidence}; values may be misread or the "
                              "document altered"))

    suspicious = [k for k in lines if injection_like(x[k])]
    if suspicious:
        found.append(add(source, "injection_suspected", True, lines[suspicious[0]], method=DERIVED,
                         confidence="high",
                         note=f"instruction-like text in {', '.join(suspicious)}; treated as data, not followed"))
    return found


# --------------------------------------------------------------------------
# Bank verification (endpoint 9)
# --------------------------------------------------------------------------

class GetBankVerificationArgs(ToolArgs):
    bank_id: Optional[str] = Field(default=None, pattern=r"^B\d+$",
                                   description="The bank verification ID from the task, e.g. B1.")


def get_bank_verification(ctx: ToolContext, args: GetBankVerificationArgs) -> ToolOutput:
    bank_ref = ctx.task.context.bank_verification
    bank_id = _allowed(ctx, "bank_id", args.bank_id, [bank_ref.id] if bank_ref else [])
    call_key = f"bank|{bank_id}"
    if (hit := ctx.cached(call_key, "get_bank_verification")):
        return hit
    bank = ctx.client.get_bank_verification(ctx.case_id, bank_id)
    if bank is None:
        raise ToolInputError(f"The case service has no bank verification {bank_id} for this case.")

    record = Record(f"Bank verification {bank.id}")
    l_holder = record.add("account_holder", bank.account_holder)
    l_status = record.add("status", bank.status)
    l_at = record.add("verified_at", bank.verified_at)
    source = ctx.add_source(call_key, source_type="bank_verification", authority=Authority.THIRD_PARTY,
                            locator=f"{ctx.case_id}/{bank.id}", rendered=record.text, source_id=bank.id)
    conf = "high" if bank.status == "verified" else "medium"
    add = ctx.add_finding
    findings = [
        add(source, "account_holder", bank.account_holder, l_holder, method=STRUCTURED, confidence=conf),
        add(source, "bank_status", bank.status, l_status, method=STRUCTURED, confidence="high"),
    ]
    if bank.verified_at:
        findings.append(add(source, "verified_at", bank.verified_at, l_at, method=STRUCTURED, confidence="high"))

    # The Lead splits "LEGAL DBA TRADE" itself when it reconciles; here the split only
    # steers the model (name_checks), so there is one owner of the name verdict.
    legal, dba = split_dba(bank.account_holder)
    ctx.name_hints(f"{bank.id}.account_holder (legal part)", legal, include_owners=True)
    if dba:
        ctx.name_hints(f"{bank.id}.account_holder (DBA part)", dba)
    return ctx.remember(call_key, output_from(ctx, "get_bank_verification", [source], findings))


# --------------------------------------------------------------------------
# Website snapshots (endpoint 10)
# --------------------------------------------------------------------------

class GetSiteIdentityArgs(ToolArgs):
    web_id: Optional[str] = Field(default=None, pattern=r"^W\d+$",
                                  description="A website snapshot ID from the task, e.g. W1.")


WEB_FINDING = {"legal_name": "website_legal_name", "dba": "website_dba", "address": "website_address"}


def get_site_identity(ctx: ToolContext, args: GetSiteIdentityArgs) -> ToolOutput:
    web_id = _allowed(ctx, "web_id", args.web_id, [w.id for w in ctx.task.context.web_snapshot])
    call_key = f"web|{web_id}"
    if (hit := ctx.cached(call_key, "get_site_identity")):
        return hit
    snap = ctx.client.get_web_snapshot(ctx.case_id, web_id)
    if snap is None:
        raise ToolInputError(f"The case service has no website snapshot {web_id} for this case.")

    record = Record(f"Website snapshot {snap.id}, fields extracted from the merchant's own site")
    l_url = record.add("url", str(snap.url))
    record.add("captured_at", snap.captured_at)
    lines = {k: record.add(f"extracted.{k}", snap.extracted[k]) for k in WEB_FINDING if k in snap.extracted}
    source = ctx.add_source(call_key, source_type="website", authority=Authority.MERCHANT,
                            locator=f"{ctx.case_id}/{snap.id}", rendered=record.text, source_id=snap.id)
    add = ctx.add_finding
    findings = [add(source, WEB_FINDING[k], snap.extracted[k], line, method=PAGE, confidence="low",
                    note="merchant's own website; unverified") for k, line in lines.items()]

    suspicious = [k for k in lines if injection_like(snap.extracted[k])]
    for key in ("legal_name", "dba"):
        if key in lines and key not in suspicious:
            ctx.name_hints(WEB_FINDING[key], snap.extracted[key])
    if "address" in lines and "address" not in suspicious:
        findings.append(add(source, "address_matches_application",
                            addresses_match(snap.extracted["address"], ctx.app.address), lines["address"],
                            method=DERIVED, confidence="low"))
    findings.append(add(source, "injection_suspected", bool(suspicious),
                        lines[suspicious[0]] if suspicious else l_url, method=DERIVED, confidence="high",
                        note=(f"instruction-like text in {', '.join(WEB_FINDING[k] for k in suspicious)}; "
                              "treated as data, not followed") if suspicious else "no instruction-like text found"))
    return ctx.remember(call_key, output_from(ctx, "get_site_identity", [source], findings))


SPECS = [
    ToolSpec("get_document",
             "Fetch one uploaded document's stored fields (already extracted by the existing system) by its "
             "D ID, compared with the application: names, last 4 of the EIN/TIN, IRS name control, dates, "
             "extraction confidence.",
             GetDocumentArgs, get_document),
    ToolSpec("get_bank_verification",
             "Fetch the bank verification result: account holder, status and date, with name_checks of its "
             "legal and DBA parts ('LEGAL DBA TRADE') against the application.",
             GetBankVerificationArgs, get_bank_verification),
    ToolSpec("get_site_identity",
             "Fetch a website snapshot's stored fields (legal name, DBA, address) by its W ID. Website values "
             "are the merchant's own unverified claims and are always low confidence.",
             GetSiteIdentityArgs, get_site_identity),
]
