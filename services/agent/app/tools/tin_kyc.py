"""Records worker tools: IRS TIN matching (endpoint 6) and owner KYC (endpoint 7).

``check_tin_match`` works like the IRS TIN Matching program: it answers Match /
Mismatch / Not Found / Not Issued for the name tested, and never says what name the
IRS has on file. The case service returns ``irs_name``; it stays inside this module.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from kyb_contracts.worker_io import Authority, ExtractionMethod

from app.shared.name_norm import best_comparison, irs_name_control

from ._common import Record, ToolArgs, ToolContext, ToolInputError, ToolOutput, ToolSpec, output_from

HIGH = "high"
STRUCTURED = ExtractionMethod.STRUCTURED_RECORD

_NOT_FETCHED = object()


class CheckTinMatchArgs(ToolArgs):
    name: str = Field(min_length=1, description="The name to test against the submitted TIN: the applied legal "
                                                "name, a former name, the DBA, or (for an SSN) the owner's name.")


def _tin_record(ctx: ToolContext):
    record = ctx.tin_record_cache.get("record", _NOT_FETCHED)
    if record is _NOT_FETCHED:
        record = ctx.client.get_tin_record(ctx.case_id)
        ctx.tin_record_cache["record"] = record
    return record


def tin_match_result(irs_status: str, irs_name: Optional[str], name: str, is_individual: bool) -> tuple[str, str]:
    """(result, name control tested). Code-only: ``irs_name`` never leaves this function."""
    tested = irs_name_control(name, is_individual=is_individual)
    if irs_status == "not_issued":
        return "Not Issued", tested
    if irs_status == "not_found" or not irs_name:
        return "Not Found", tested
    on_file = irs_name_control(irs_name, is_individual=is_individual)
    return ("Match" if tested and tested == on_file else "Mismatch"), tested


def check_tin_match(ctx: ToolContext, args: CheckTinMatchArgs) -> ToolOutput:
    name = args.name.strip()
    call_key = f"tin|{name.casefold()}"
    if (hit := ctx.cached(call_key, "check_tin_match")):
        return hit
    record = _tin_record(ctx)
    if record is None:
        raise ToolInputError("The case has no submitted TIN, so there is nothing to test.")

    is_individual = record.tin_type == "SSN"
    result, tested = tin_match_result(record.irs_status, record.irs_name, name, is_individual)

    # The IRS compares only the name control, so a name with the same control as one already
    # tested gets the same answer: hand back that result instead of a new R source.
    control_key = f"tin-control|{tested}"
    if (hit := ctx.cached(control_key, "check_tin_match")):
        hit.note = (f"{name!r} has the same IRS name control ({tested}) as a name already tested in this "
                    "task, so the result is the same; these are the earlier findings. To learn more, test "
                    "a name with a different name control.")
        return hit

    rendered = Record(f"IRS TIN match for case {ctx.case_id}")
    l_type = rendered.add("tin_type", record.tin_type)
    l_last4 = rendered.add("tin_last4", record.tin_last4)
    rendered.add("name_tested", name)
    l_nc = rendered.add("name_control_tested", tested)
    l_result = rendered.add("result", result)
    source = ctx.add_source(call_key, source_type="irs_tin_match", authority=Authority.OFFICIAL,
                            locator=f"tin-record:{ctx.case_id} name={name}", rendered=rendered.text)

    subject = next((o.ref for o in ctx.app.owners if best_comparison(name, o.name).at_least("normalized_equal")),
                   "business")
    rule = "individual (last name)" if is_individual else "business"
    notes = {
        "Match": f"the IRS name on file matches {name!r} ({rule} name-control rule)",
        "Mismatch": f"the IRS name on file does not match {name!r}; the IRS name itself is not disclosed",
        "Not Issued": "the IRS has not issued this TIN yet; common for an EIN applied for in the last few weeks",
        "Not Found": "the IRS has no record of this TIN",
    }
    add = ctx.add_finding
    findings = [
        add(source, "tin_match_result", result, l_result, method=STRUCTURED, confidence=HIGH, subject=subject,
            note=notes[result]),
        add(source, "name_control_tested", tested, l_nc, method=STRUCTURED, confidence=HIGH, subject=subject,
            note=f"name tested: {name}"),
        add(source, "tin_type", record.tin_type, l_type, method=STRUCTURED, confidence=HIGH, subject=subject),
        add(source, "tin_last4", record.tin_last4, l_last4, method=STRUCTURED, confidence=HIGH, subject=subject),
    ]
    output = ctx.remember(call_key, output_from(ctx, "check_tin_match", [source], findings))
    return ctx.remember(control_key, output)


class GetKycResultArgs(ToolArgs):
    owner_ref: Optional[str] = Field(default=None, pattern=r"^owner:P\d+$",
                                     description="The owner, e.g. owner:P1. Optional when the case has one KYC check.")


def get_kyc_result(ctx: ToolContext, args: GetKycResultArgs) -> ToolOutput:
    refs = ctx.task.context.owner_kyc
    if args.owner_ref is None:
        if len(refs) != 1:
            raise ToolInputError(f"owner_ref is required; this case has KYC for {[k.owner_ref for k in refs]}")
        ref = refs[0]
    else:
        ref = next((k for k in refs if k.owner_ref == args.owner_ref), None)
        if ref is None:
            raise ToolInputError(f"No KYC check for {args.owner_ref} in this case; "
                                 f"available: {[k.owner_ref for k in refs] or 'none'}")
    call_key = f"kyc|{ref.id}"
    if (hit := ctx.cached(call_key, "get_kyc_result")):
        return hit

    kyc = ctx.client.get_owner_kyc(ctx.case_id, ref.id)
    if kyc is None:
        raise ToolInputError(f"The case service has no KYC result {ref.id} for this case.")
    rendered = Record(f"Owner KYC {kyc.id} for {kyc.owner_ref}")
    rendered.add("owner_ref", kyc.owner_ref)
    line = rendered.add("result", kyc.result)
    source = ctx.add_source(call_key, source_type="owner_kyc", authority=Authority.THIRD_PARTY,
                            locator=f"{ctx.case_id}/{kyc.id}", rendered=rendered.text, source_id=kyc.id)
    finding = ctx.add_finding(source, "kyc_result", kyc.result, line, method=STRUCTURED, confidence=HIGH,
                              subject=kyc.owner_ref)
    return ctx.remember(call_key, output_from(ctx, "get_kyc_result", [source], [finding]))


SPECS = [
    ToolSpec("check_tin_match",
             "Test one name against the submitted TIN, like IRS TIN Matching: returns Match, Mismatch, "
             "Not Found or Not Issued and the name control tested. The IRS name on file is never shown. "
             "One call per name; on a Mismatch, test other candidate names (former names, the DBA, or the "
             "owner's name when the TIN is an SSN).",
             CheckTinMatchArgs, check_tin_match),
    ToolSpec("get_kyc_result",
             "Fetch an owner's identity (KYC) check result: pass, fail or pending.",
             GetKycResultArgs, get_kyc_result),
]
