"""Steps the Records worker still owes before it may answer a first task.

gpt-4o-mini tends to answer right after its first batch of lookups, even when those
results call for a next step (a TIN Mismatch next to a known former name). When the model
stops, the worker loop asks ``missing_steps``; if anything is listed, it sends the list to
the model once and lets it continue. Code never runs a tool itself: the model still
chooses the calls, within the same budget.

Only first tasks get the check: a follow-up asks for one specific lookup.
"""

from __future__ import annotations

from app.shared.name_norm import best_comparison, irs_name_control
from app.tools._common import ToolContext

TIN_FLAGS = {"TIN_MISMATCH", "TIN_NOT_FOUND"}


def _same_name(a: str, b: str) -> bool:
    return best_comparison(a, b).at_least("normalized_equal")


def _candidate_names(ctx: ToolContext) -> list[str]:
    """Other names the business may have used: former names and a DBA registrant that is
    neither the applied legal name nor an owner (a sole proprietor's DBA names the owner)."""
    app = ctx.app
    names: list[str] = []
    for f in ctx.findings:
        if f.field == "former_names" and isinstance(f.value, list):
            names += [str(v) for v in f.value]
        elif f.field == "former_name" and isinstance(f.value, str):
            names.append(f.value)
        elif f.field == "registrant" and isinstance(f.value, str):
            if not _same_name(f.value, app.legal_name) and not any(_same_name(f.value, o.name) for o in app.owners):
                names.append(f.value)
    out: list[str] = []
    for n in names:
        if n and not _same_name(n, app.legal_name) and not any(_same_name(n, m) for m in out):
            out.append(n)
    return out


def missing_steps(ctx: ToolContext) -> list[str]:
    """The lookups the results so far call for and that haven't run, as short instructions."""
    task = ctx.task
    if task.worker != "records" or task.follow_up_of:
        return []
    app = ctx.app
    flags = {str(getattr(f, "value", f)) for f in task.context.flag_reasons}
    fields = {f.field for f in ctx.findings}
    source_types = {s.source_type for s in ctx.sources.values()}
    steps: list[str] = []

    tin_flagged = bool(flags & TIN_FLAGS)
    if tin_flagged and "tin_match_result" not in fields:
        steps.append(f"check_tin_match with the applied legal name {app.legal_name!r} (the TIN is flagged)")

    candidates = _candidate_names(ctx)
    if candidates and "sos_registry" in source_types and "sos_filing_history" not in source_types:
        steps.append(f"get_filing_history, for the dated name change behind {', '.join(map(repr, candidates))}")

    results = [f.value for f in ctx.findings if f.field == "tin_match_result"]
    is_ssn = str(getattr(app.tin_type, "value", app.tin_type)) == "SSN"
    if tin_flagged and "Mismatch" in results and "Match" not in results and not is_ssn:
        tested = {f.value for f in ctx.findings if f.field == "name_control_tested"}
        for name in candidates:
            control = irs_name_control(name)
            if control and control not in tested:
                steps.append(f"check_tin_match with {name!r} (name control {control}, not tested yet)")
                tested.add(control)
    return steps


def reminder(steps: list[str]) -> str:
    return ("Before you answer: the results so far call for these lookups, which haven't run. Make them "
            "now (several in one turn is fine), then answer:\n" + "\n".join(f"- {s}" for s in steps))


__all__ = ["missing_steps", "reminder"]
