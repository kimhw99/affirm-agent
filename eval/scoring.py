"""Score one agent review against a golden case's expected.json (kyb_contracts.golden.ExpectedAnswer).

Pure functions, no I/O: ``score_case`` takes the expected answer, the agent's
``review_output`` (app.review.ReviewOutput as a dict) and the workers' results
(WorkerResult dicts), and returns a dict of verdicts.

What is scored
--------------
- decision: memo.recommendation == expected_decision (or in acceptable_decisions).
- must_cite: each item is cited by the memo (flag evidence or ruled_out evidence).
  * "D1" style IDs must appear in the memo's citations.
  * R records are RecordRefs, {source: <source_type>, where: {<finding field>: <value>}};
    a cited R ID matches when its Source has that source_type and, for each field in
    ``where``, one of the source's findings has that field and value.
  * {any_of: [...]} needs at least one.
  Each item also records whether the workers *retrieved* it at all, which separates
  "never looked it up" from "found it but didn't cite it".
- forbidden_actions: the ones a rule can decide (decision-shaped ones, flags the agent
  must leave alone) are checked here; the rest are left for the optional LLM judge.
- follow-up rounds vs max_follow_up_rounds, and whether step-9 checks passed.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

DECISIONS = ("recommend_clear", "request_documents", "escalate", "insufficient_evidence")


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().casefold()


def _value_matches(want: Any, found: Any) -> bool:
    """A finding's value equals the expected one; strings ignore case and extra spaces.
    Other values (such as a filing_event dict) must be equal as written."""
    if isinstance(want, str) and isinstance(found, str):
        return _norm(found) == _norm(want)
    return found == want


def index_sources(worker_results: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """source_id -> {"source": Source dict, "findings": [Finding dicts]} across all rounds."""
    out: dict[str, dict[str, Any]] = {}
    for result in worker_results:
        for src in result.get("sources") or []:
            out.setdefault(src["source_id"], {"source": src, "findings": []})
        for f in result.get("findings") or []:
            out.setdefault(f["source_id"], {"source": {"source_id": f["source_id"], "source_type": "?",
                                                       "locator": ""}, "findings": []})
            out[f["source_id"]]["findings"].append(f)
    return out


def record_matches(ref: dict[str, Any], entry: dict[str, Any]) -> bool:
    """RecordRef rule (contracts/kyb_contracts/golden.py): the source has ref's source_type and,
    for each field in ``where``, one of its findings has that field with that value."""
    if entry["source"].get("source_type") != ref["source"]:
        return False
    return all(any(f["field"] == field and _value_matches(want, f["value"]) for f in entry["findings"])
               for field, want in (ref.get("where") or {}).items())


def _item_hits(item: Any, ids: set[str], sources: dict[str, dict[str, Any]]) -> bool:
    if isinstance(item, str):
        return item in ids
    if "any_of" in item:
        return any(_item_hits(i, ids, sources) for i in item["any_of"])
    return any(record_matches(item, sources[i]) for i in ids if i.startswith("R") and i in sources)


def _describe(item: Any) -> str:
    if isinstance(item, str):
        return item
    if "any_of" in item:
        return "any_of(" + " | ".join(_describe(i) for i in item["any_of"]) + ")"
    rest = ", ".join(f"{k}={v}" for k, v in (item.get("where") or {}).items())
    return f"R:{item['source']}" + (f"[{rest}]" if rest else "")


def memo_citations(memo: Optional[dict[str, Any]]) -> set[str]:
    if not memo:
        return set()
    ids = {i for o in memo.get("flags") or [] for i in o.get("evidence") or []}
    return ids | {i for c in memo.get("ruled_out") or [] for i in c.get("evidence") or []}


def _all_text(memo: Optional[dict[str, Any]]) -> str:
    if not memo:
        return ""
    parts = [memo.get("notes_for_analyst") or ""]
    parts += [o.get("root_cause") or "" for o in memo.get("flags") or []]
    parts += [c.get("cause") or "" for c in memo.get("ruled_out") or []]
    parts += [d.get("reason") or "" for d in memo.get("document_request") or []]
    return "\n".join(parts)


def rule_check_forbidden(action: str, memo: Optional[dict[str, Any]], expected_decision: str) -> Any:
    """True = violated, False = respected, None = a rule can't decide (needs the judge), "n/a" = not applicable."""
    rec = (memo or {}).get("recommendation", "insufficient_evidence")
    a = action.strip()
    low = a.casefold()

    if "shared_facts" in low:
        return "n/a"   # the agent has no shared_facts structure, so this can't happen as written
    if low == "recommend_clear":
        return rec == "recommend_clear"
    m = re.match(r"resolve or comment on ([A-Z_]+)$", a)
    if m:
        flag = m.group(1)
        # Violated if the agent reviewed the flag (put it in memo.flags) or discussed it in its own
        # words. The code-written "The agent did not review FLAG." line doesn't count.
        in_flags = any(o.get("flag") == flag for o in (memo or {}).get("flags") or [])
        text = _all_text(memo).replace(f"The agent did not review {flag}.", "")
        return in_flags or flag in text
    # A forbidden action that starts with a decision is violated only if the agent made that
    # decision; whether it made it *for that reason* is the judge's call. If the decision differs,
    # it is certainly respected.
    if low.startswith("escalate") and rec != "escalate":
        return False
    if low.startswith("recommend_clear") and rec != "recommend_clear":
        return False
    if low.startswith("request") and rec != "request_documents" and not (memo or {}).get("document_request"):
        return False
    if low.startswith("escalate") and rec == "escalate" and expected_decision != "escalate":
        return True   # escalating at all was wrong for this case, so any escalate-* prohibition is hit
    if low.startswith("recommend_clear") and rec == "recommend_clear" and expected_decision != "recommend_clear":
        return True
    return None


def score_case(expected: dict[str, Any], review_output: Optional[dict[str, Any]],
               worker_results: list[dict[str, Any]], internal_rounds: int) -> dict[str, Any]:
    checked = (review_output or {}).get("checked_memo") or {}
    memo = checked.get("memo")
    rec = memo.get("recommendation") if memo else None
    ok_decisions = [expected["expected_decision"], *(expected.get("acceptable_decisions") or [])]
    ids = memo_citations(memo)
    sources = index_sources(worker_results)
    case_fixed_ids = set()   # D/B/W/K are always "retrievable": they are in the case

    cite_rows = []
    for item in expected.get("must_cite") or []:
        cited = _item_hits(item, ids, sources)
        retrieved = cited or _item_hits(item, set(sources) | _fixed(item, case_fixed_ids), sources)
        cite_rows.append({"item": _describe(item), "cited": cited, "retrieved": retrieved})
    not_cite_rows = [{"item": _describe(i), "cited": _item_hits(i, ids, sources)}
                     for i in expected.get("must_not_cite") or []]

    forbidden = []
    for action in expected.get("forbidden_actions") or []:
        forbidden.append({"action": action,
                          "violated": rule_check_forbidden(action, memo, expected["expected_decision"]),
                          "by": "rule"})

    check_result = checked.get("check_result") or {}
    lead_failures = (review_output or {}).get("lead_check_failures") or []
    return {
        "decision": rec,
        "decision_correct": rec in ok_decisions,
        "must_cite": cite_rows,
        "must_cite_recall": (sum(r["cited"] for r in cite_rows) / len(cite_rows)) if cite_rows else 1.0,
        "must_cite_all": all(r["cited"] for r in cite_rows),
        "must_not_cite": not_cite_rows,
        "forbidden": forbidden,
        "internal_rounds": internal_rounds,
        "rounds_ok": internal_rounds <= (expected.get("max_follow_up_rounds") or 99),
        "checks_passed": bool(check_result.get("passed")) and not lead_failures,
        "check_failures": [f.get("rule") for f in check_result.get("failures") or []]
                          + [f.get("rule") for f in lead_failures],
        "auto_resolved": list((memo or {}).get("auto_resolved") or []),
        "out_of_scope": list((memo or {}).get("out_of_scope") or []),
        "memo_flags": [{"flag": o.get("flag"), "status": o.get("status"), "evidence": o.get("evidence")}
                       for o in (memo or {}).get("flags") or []],
        "document_request": [d.get("type") for d in (memo or {}).get("document_request") or []],
        "cited_ids": sorted(ids),
    }


def _fixed(item: Any, _unused: set[str]) -> set[str]:
    """D/B/W/K IDs named in an item: they exist in the case, so they count as retrievable."""
    if isinstance(item, str):
        return {item}
    if isinstance(item, dict) and "any_of" in item:
        return set().union(*(_fixed(i, _unused) for i in item["any_of"]))
    return set()
