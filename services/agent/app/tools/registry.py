"""Records worker tools over the mocked registry, DBA and commercial databases (endpoints 1-5)."""

from __future__ import annotations

from typing import Optional

from pydantic import Field, model_validator
from rapidfuzz import fuzz

from kyb_contracts.worker_io import Authority, ExtractionMethod, Finding, Source
from kyb_contracts.world import CommercialProfile, DbaFiling, Filing, RegistryEntity

from app.shared.name_norm import LABEL_RANK, best_comparison, match_dba

from ._common import (
    Record,
    ToolArgs,
    ToolContext,
    ToolInputError,
    ToolOutput,
    ToolSpec,
    output_from,
    parse_date,
)

HIGH = "high"
STRUCTURED = ExtractionMethod.STRUCTURED_RECORD
DERIVED = ExtractionMethod.DERIVED
MAX_CANDIDATES = 3


def _state(ctx: ToolContext, state: Optional[str]) -> str:
    return (state or ctx.app.state).strip().upper()


def _key(*parts: object) -> str:
    return "|".join("" if p is None else str(p).casefold() for p in parts)


# --------------------------------------------------------------------------
# Registry entities (endpoints 1 and 2)
# --------------------------------------------------------------------------

def _entity_source(ctx: ToolContext, entity: RegistryEntity) -> tuple[Source, dict[str, str], list[str]]:
    record = Record(f"{entity.state} Secretary of State registry entity {entity.state_entity_id}")
    lines = {
        "state_entity_id": record.add("state_entity_id", entity.state_entity_id),
        "legal_name": record.add("legal_name", entity.legal_name),
        "former_names": record.add("former_names", entity.former_names),
        "entity_type": record.add("entity_type", entity.entity_type),
        "entity_status": record.add("entity_status", entity.entity_status),
        "formation_date": record.add("formation_date", entity.formation_date),
        "registered_address": record.add("registered_address", entity.registered_address),
    }
    officer_lines = [record.add("officer", f"{o.name} ({o.title})") for o in entity.officers]
    source = ctx.add_source(
        _key("sos", entity.state, entity.state_entity_id), source_type="sos_registry",
        authority=Authority.OFFICIAL, locator=f"{entity.state} SOS {entity.state_entity_id}",
        rendered=record.text)
    return source, lines, officer_lines


def _entity_findings(ctx: ToolContext, entity: RegistryEntity, *, is_applicant: bool) -> tuple[Source, list[Finding]]:
    source, lines, officer_lines = _entity_source(ctx, entity)
    add = ctx.add_finding
    found = [
        add(source, "legal_name", entity.legal_name, lines["legal_name"], method=STRUCTURED, confidence=HIGH),
        add(source, "state_entity_id", entity.state_entity_id, lines["state_entity_id"], method=STRUCTURED,
            confidence=HIGH),
        add(source, "entity_type", entity.entity_type, lines["entity_type"], method=STRUCTURED, confidence=HIGH),
        add(source, "entity_status", entity.entity_status, lines["entity_status"], method=STRUCTURED,
            confidence=HIGH),
        add(source, "registered_address", entity.registered_address, lines["registered_address"],
            method=STRUCTURED, confidence=HIGH),
    ]
    if entity.formation_date:
        found.append(add(source, "formation_date", entity.formation_date, lines["formation_date"],
                         method=STRUCTURED, confidence=HIGH))
    for officer, line in zip(entity.officers, officer_lines):
        subject = next((o.ref for o in ctx.app.owners
                        if best_comparison(o.name, officer.name).at_least("normalized_equal")), "business")
        found.append(add(source, "officer_name", officer.name, line, method=STRUCTURED, confidence=HIGH,
                         subject=subject, note=f"title: {officer.title}"))
    if entity.former_names:
        found.append(add(source, "former_names", entity.former_names, lines["former_names"],
                         method=STRUCTURED, confidence=HIGH))

    comparisons = ctx.add_name_comparisons(source, entity.legal_name, lines["legal_name"],
                                           confidence=HIGH, label=f"{source.source_id}.legal_name")
    found += comparisons
    # Remember the applicant's former names for later comparisons and TIN tests.
    if is_applicant or any(f.value["against"] == "application.legal_name"
                           and LABEL_RANK[f.value["label"]] >= LABEL_RANK["core_equal_suffix_differs"]
                           for f in comparisons):
        ctx.former_names.extend(n for n in entity.former_names if n not in ctx.former_names)
    return source, found


class SearchRegistryArgs(ToolArgs):
    name: Optional[str] = Field(default=None, description="Business name to search for.")
    officer: Optional[str] = Field(default=None, description="Officer or member name to search for.")
    address: Optional[str] = Field(default=None, description="Registered address to search for.")
    state: Optional[str] = Field(default=None, description="Two-letter state; defaults to the application's state.")


def _rank(ctx: ToolContext, entity: RegistryEntity, args: SearchRegistryArgs) -> tuple:
    if args.name or not (args.officer or args.address):
        names = [args.name or ctx.app.legal_name, ctx.app.legal_name] + ([ctx.app.dba] if ctx.app.dba else [])
        best = max((best_comparison(entity.legal_name, n) for n in names),
                   key=lambda c: (LABEL_RANK[c.label], c.score))
        return (LABEL_RANK[best.label], best.score)
    if args.officer:
        return (0, max((fuzz.token_sort_ratio(args.officer.casefold(), o.name.casefold())
                        for o in entity.officers), default=0.0))
    return (0, fuzz.token_set_ratio((args.address or "").casefold(), entity.registered_address.casefold()))


def search_registry(ctx: ToolContext, args: SearchRegistryArgs) -> ToolOutput:
    state = _state(ctx, args.state)
    if not (args.name or args.officer or args.address):
        args = args.model_copy(update={"name": ctx.app.legal_name})
    call_key = _key("search_registry", state, args.name, args.officer, args.address)
    if (hit := ctx.cached(call_key, "search_registry")):
        return hit

    results = ctx.client.search_registry(state, name=args.name, officer=args.officer, address=args.address)
    query = ", ".join(f"{k}={v}" for k, v in (("name", args.name), ("officer", args.officer),
                                              ("address", args.address)) if v)
    if not results:
        record = Record(f"{state} Secretary of State registry search")
        record.add("query", query)
        line_r = record.add("results", 0)
        source = ctx.add_source(call_key, source_type="sos_registry_search", authority=Authority.OFFICIAL,
                                locator=f"{state} SOS search ({query})", rendered=record.text)
        note = f"No {state} registry entity for {query}"
        if ctx.app.entity_type == "SOLE_PROPRIETORSHIP":
            note += "; expected for a sole proprietor, who is never in the registry"
        finding = ctx.add_finding(source, "registry_match", "none", line_r, method=STRUCTURED,
                                  confidence=HIGH, note=note)
        return ctx.remember(call_key, output_from(ctx, "search_registry", [source], [finding]))

    ranked = sorted(results, key=lambda e: _rank(ctx, e, args), reverse=True)[:MAX_CANDIDATES]
    sources, findings = [], []
    for entity in ranked:
        is_applicant = (entity.state_entity_id == ctx.app.state_entity_id and entity.state == ctx.app.state)
        source, found = _entity_findings(ctx, entity, is_applicant=is_applicant)
        sources.append(source)
        findings += found
    note = f"{len(results)} registry result(s) for {query}; showing the {len(ranked)} closest."
    return ctx.remember(call_key, output_from(ctx, "search_registry", sources, findings, note))


class GetRegistryEntityArgs(ToolArgs):
    state: Optional[str] = Field(default=None, description="Two-letter state; defaults to the application's.")
    state_entity_id: Optional[str] = Field(default=None,
                                           description="State entity ID; defaults to the application's.")


def get_registry_entity(ctx: ToolContext, args: GetRegistryEntityArgs) -> ToolOutput:
    state = _state(ctx, args.state)
    entity_id = args.state_entity_id or ctx.app.state_entity_id
    if not entity_id:
        raise ToolInputError("The application has no state_entity_id; use search_registry instead.")
    call_key = _key("get_registry_entity", state, entity_id)
    if (hit := ctx.cached(call_key, "get_registry_entity")):
        return hit

    entity = ctx.client.get_registry_entity(state, entity_id)
    if entity is None:
        record = Record(f"{state} Secretary of State registry lookup")
        record.add("state_entity_id", entity_id)
        line = record.add("result", "not found")
        source = ctx.add_source(call_key, source_type="sos_registry", authority=Authority.OFFICIAL,
                                locator=f"{state} SOS {entity_id}", rendered=record.text)
        finding = ctx.add_finding(source, "registry_entity", "not_found", line, method=STRUCTURED,
                                  confidence=HIGH, note=f"No {state} registry entity with ID {entity_id}")
        return ctx.remember(call_key, output_from(ctx, "get_registry_entity", [source], [finding]))

    is_applicant = entity_id == ctx.app.state_entity_id and state == ctx.app.state
    source, findings = _entity_findings(ctx, entity, is_applicant=is_applicant)
    if entity.formation_date:
        days = (ctx.today - entity.formation_date).days
        line = next(l for l in ctx.rendered[source.source_id].splitlines() if l.startswith("formation_date:"))
        findings.append(ctx.add_finding(source, "formed_days_before_review", days, line, method=DERIVED,
                                        confidence=HIGH, note=f"formed {days} day(s) before {ctx.today}"))
    return ctx.remember(call_key, output_from(ctx, "get_registry_entity", [source], findings))


# --------------------------------------------------------------------------
# Filing history (endpoint 3)
# --------------------------------------------------------------------------

def _event_text(f: Filing) -> str:
    return f"{f.event_type} {f.effective_date.isoformat()}: {f.old_value or '(none)'} -> {f.new_value or '(none)'}" \
           f" [{f.event_id}]"


def get_filing_history(ctx: ToolContext, args: GetRegistryEntityArgs) -> ToolOutput:
    state = _state(ctx, args.state)
    entity_id = args.state_entity_id or ctx.app.state_entity_id
    if not entity_id:
        raise ToolInputError("The application has no state_entity_id; find the entity with search_registry first.")
    call_key = _key("filings", state, entity_id)
    if (hit := ctx.cached(call_key, "get_filing_history")):
        return hit

    filings = ctx.client.get_filings(state, entity_id)
    record = Record(f"{state} Secretary of State filing history for {entity_id}")
    record.add("state_entity_id", entity_id)
    if filings is None:
        line = record.add("result", "entity not found")
        value = "entity_not_found"
    elif not filings:
        line = record.add("events", 0)
        value = "none"
    event_lines = [record.add("event", _event_text(f)) for f in (filings or [])]
    source = ctx.add_source(call_key, source_type="sos_filing_history", authority=Authority.OFFICIAL,
                            locator=f"{state} SOS {entity_id} filings", rendered=record.text)
    if not filings:
        note = ("No filings on record; usual for a newly formed entity." if value == "none"
                else f"No {state} registry entity with ID {entity_id}.")
        finding = ctx.add_finding(source, "filing_history", value, line, method=STRUCTURED, confidence=HIGH,
                                  note=note)
        return ctx.remember(call_key, output_from(ctx, "get_filing_history", [source], [finding]))

    findings = []
    for filing, line in zip(filings, event_lines):
        findings.append(ctx.add_finding(
            source, "filing_event",
            {"event_id": filing.event_id, "event_type": filing.event_type,
             "effective_date": filing.effective_date.isoformat(),
             "old_value": filing.old_value, "new_value": filing.new_value},
            line, method=STRUCTURED, confidence=HIGH))
        if filing.event_type == "name_change" and filing.old_value:
            findings.append(ctx.add_finding(
                source, "former_name", filing.old_value, line, method=DERIVED, confidence=HIGH,
                note=f"name used until {filing.effective_date.isoformat()}; worth testing with check_tin_match"))
            if filing.old_value not in ctx.former_names:
                ctx.former_names.append(filing.old_value)
    return ctx.remember(call_key, output_from(ctx, "get_filing_history", [source], findings))


# --------------------------------------------------------------------------
# DBA filings (endpoint 4)
# --------------------------------------------------------------------------

class SearchDbaArgs(ToolArgs):
    name: str = Field(min_length=1, description="The trade name (DBA) to look for.")
    county: Optional[str] = Field(default=None, description="County to search; omit to search the state "
                                                              "and every county.")
    state: Optional[str] = Field(default=None, description="Two-letter state; defaults to the application's.")


def _dba_source(ctx: ToolContext, filing: DbaFiling) -> tuple[Source, dict[str, str]]:
    where = f"{filing.county} County, {filing.state}" if filing.county else f"{filing.state} (state level)"
    record = Record(f"Assumed-name (DBA) filing {filing.filing_id}, {where}")
    lines = {
        "filing_id": record.add("filing_id", filing.filing_id),
        "dba": record.add("dba", filing.dba),
        "registrant": record.add("registrant", filing.registrant),
        "registrant_type": record.add("registrant_type", filing.registrant_type),
        "registrant_state_entity_id": record.add("registrant_state_entity_id", filing.registrant_state_entity_id),
        "filed": record.add("filed", filing.filed),
        "expires": record.add("expires", filing.expires),
        "status": record.add("status", filing.status),
    }
    source = ctx.add_source(_key("dba", filing.filing_id), source_type="county_dba" if filing.county else "state_dba",
                            authority=Authority.OFFICIAL, locator=filing.filing_id, rendered=record.text)
    return source, lines


def search_dba(ctx: ToolContext, args: SearchDbaArgs) -> ToolOutput:
    state = _state(ctx, args.state)
    call_key = _key("search_dba", state, args.county, args.name)
    if (hit := ctx.cached(call_key, "search_dba")):
        return hit

    results = ctx.client.search_dba(state, args.name, county=args.county)
    matches = match_dba(args.name, results, min_label="core_equal_suffix_differs")
    where = f"{args.county} County, {state}" if args.county else f"{state} and its counties"
    if not matches:
        record = Record(f"Assumed-name (DBA) search, {where}")
        record.add("name", args.name)
        line = record.add("matching_filings", 0)
        source = ctx.add_source(call_key, source_type="dba_search", authority=Authority.OFFICIAL,
                                locator=f"DBA search {where} ({args.name})", rendered=record.text)
        note = f"No DBA filing for {args.name!r} in {where}"
        if results:
            note += f"; {len(results)} filing(s) with only loosely similar names were ignored"
        finding = ctx.add_finding(source, "dba_match", "none", line, method=STRUCTURED, confidence=HIGH, note=note)
        return ctx.remember(call_key, output_from(ctx, "search_dba", [source], [finding]))

    sources, findings = [], []
    add = ctx.add_finding
    for filing in matches:
        source, lines = _dba_source(ctx, filing)
        sources.append(source)
        findings += [
            add(source, "dba", filing.dba, lines["dba"], method=STRUCTURED, confidence=HIGH),
            add(source, "registrant", filing.registrant, lines["registrant"], method=STRUCTURED, confidence=HIGH),
            add(source, "registrant_type", filing.registrant_type, lines["registrant_type"], method=STRUCTURED,
                confidence=HIGH),
            add(source, "filed", filing.filed, lines["filed"], method=STRUCTURED, confidence=HIGH),
            add(source, "dba_status", filing.status, lines["status"], method=STRUCTURED, confidence=HIGH),
        ]
        if filing.registrant_state_entity_id:
            findings.append(add(source, "registrant_state_entity_id", filing.registrant_state_entity_id,
                                lines["registrant_state_entity_id"], method=STRUCTURED, confidence=HIGH))
        if filing.expires:
            findings.append(add(source, "expires", filing.expires, lines["expires"], method=STRUCTURED,
                                confidence=HIGH))
        expired = filing.status == "expired" or (filing.expires is not None and filing.expires < ctx.today)
        findings.append(add(source, "dba_expired", expired,
                            lines["expires"] if filing.expires else lines["status"],
                            method=DERIVED, confidence=HIGH))
        findings += ctx.add_name_comparisons(source, filing.dba, lines["dba"], confidence=HIGH,
                                             label=f"{source.source_id}.dba")
        findings += ctx.add_name_comparisons(source, filing.registrant, lines["registrant"], confidence=HIGH,
                                             label=f"{source.source_id}.registrant", include_owners=True)
    return ctx.remember(call_key, output_from(ctx, "search_dba", sources, findings))


# --------------------------------------------------------------------------
# Commercial database (endpoint 5)
# --------------------------------------------------------------------------

class SearchCommercialArgs(ToolArgs):
    name: Optional[str] = Field(default=None, description="Business name; defaults to the applied legal name "
                                                          "when no address is given.")
    address: Optional[str] = Field(default=None, description="Business address.")
    state: Optional[str] = Field(default=None, description="Two-letter state; defaults to the application's.")


def search_commercial_db(ctx: ToolContext, args: SearchCommercialArgs) -> ToolOutput:
    state = _state(ctx, args.state)
    if not (args.name or args.address):
        args = args.model_copy(update={"name": ctx.app.legal_name})
    call_key = _key("commercial", state, args.name, args.address)
    if (hit := ctx.cached(call_key, "search_commercial_db")):
        return hit

    results = ctx.client.search_commercial(state, name=args.name, address=args.address)
    query = ", ".join(f"{k}={v}" for k, v in (("name", args.name), ("address", args.address)) if v)
    if not results:
        record = Record(f"Commercial database search, {state}")
        record.add("query", query)
        line = record.add("results", 0)
        source = ctx.add_source(call_key, source_type="commercial_db_search", authority=Authority.THIRD_PARTY,
                                locator=f"commercial search {state} ({query})", rendered=record.text)
        note = f"No commercial profile for {query}"
        formed = ctx.app.formation_date
        if formed and (ctx.today - formed).days < 180:
            note += f"; the business was formed {(ctx.today - formed).days} day(s) ago, so this is expected"
        finding = ctx.add_finding(source, "commercial_match", "none", line, method=STRUCTURED, confidence=HIGH,
                                  note=note)
        return ctx.remember(call_key, output_from(ctx, "search_commercial_db", [source], [finding]))

    def rank(p: CommercialProfile) -> tuple:
        target = args.name or ctx.app.legal_name
        c = best_comparison(p.business_name, target)
        return (LABEL_RANK[c.label], c.score)

    ranked = sorted(results, key=rank, reverse=True)[:MAX_CANDIDATES]
    sources, findings = [], []
    add = ctx.add_finding
    for profile in ranked:
        record = Record(f"Commercial database profile {profile.profile_id}")
        record.add("profile_id", profile.profile_id)
        l_name = record.add("business_name", profile.business_name)
        l_addr = record.add("address", profile.address)
        l_first = record.add("first_seen", profile.first_seen)
        l_upd = record.add("last_updated", profile.last_updated)
        source = ctx.add_source(_key("cdb", profile.profile_id), source_type="commercial_db",
                                authority=Authority.THIRD_PARTY, locator=profile.profile_id, rendered=record.text)
        sources.append(source)
        findings += [
            add(source, "business_name", profile.business_name, l_name, method=STRUCTURED, confidence=HIGH),
            add(source, "address", profile.address, l_addr, method=STRUCTURED, confidence=HIGH),
            add(source, "first_seen", profile.first_seen, l_first, method=STRUCTURED, confidence=HIGH),
            add(source, "last_updated", profile.last_updated, l_upd, method=STRUCTURED, confidence=HIGH),
        ]
        findings += ctx.add_name_comparisons(source, profile.business_name, l_name, confidence=HIGH,
                                             label=f"{source.source_id}.business_name")
        formed = ctx.app.formation_date
        if formed:
            findings.append(add(source, "first_seen_after_formation", profile.first_seen >= formed, l_first,
                                method=DERIVED, confidence=HIGH,
                                note=f"application formation_date {formed.isoformat()}"))
    note = f"{len(results)} commercial profile(s) for {query}; showing the {len(ranked)} closest."
    return ctx.remember(call_key, output_from(ctx, "search_commercial_db", sources, findings, note))


SPECS = [
    ToolSpec("search_registry",
             "Search the Secretary of State registry by business name, officer or address (broad match; "
             "returns the 3 closest entities, each compared with the application's names). Use it when the "
             "application has no state_entity_id or to look for look-alike entities.",
             SearchRegistryArgs, search_registry),
    ToolSpec("get_registry_entity",
             "Fetch one registry entity by state and state_entity_id (defaults to the application's): legal "
             "name, former names, type, status, formation date, address, officers.",
             GetRegistryEntityArgs, get_registry_entity),
    ToolSpec("get_filing_history",
             "Fetch an entity's filing history (formation, name changes, dissolution...), oldest first. "
             "Defaults to the application's entity. Name changes are reported as former names.",
             GetRegistryEntityArgs, get_filing_history),
    ToolSpec("search_dba",
             "Search assumed-name (DBA) filings for a trade name, in the state and optionally one county. "
             "Reports who registered each matching DBA and compares the registrant with the application.",
             SearchDbaArgs, search_dba),
    ToolSpec("search_commercial_db",
             "Search the commercial database (LexisNexis / D&B style) by name or address. No record is "
             "normal for a brand-new business.",
             SearchCommercialArgs, search_commercial_db),
]
