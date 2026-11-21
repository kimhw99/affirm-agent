"""The whole graph, end to end, with scripted workers and no LLM (or a stub LLM)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from langgraph.types import Command

from case_service_fake import FakeCaseService
from conftest import NOW, TODAY
from fakes import (
    ScriptedWorker,
    bank,
    document,
    evidence_1042,
    evidence_2001,
    evidence_3001,
    kyc,
    records_1042,
    records_2001,
    records_3001,
    registry_none,
    tin_match,
    website,
)
from kyb_contracts.memo import CheckedMemo
from app.review import ReviewOutput

from app.graph.followup import MAX_INTERNAL_ROUNDS
from app.graph.llm import ExplainOut, NullLeadLLM
from app.graph.plan import AddedCheck, PlanExtras
from app.graph.workflow import Deps, build_graph
from app.main import create_app


def _deps(records, evidence, llm=None, sink=None):
    return Deps(ScriptedWorker("records", records), ScriptedWorker("evidence", evidence),
                llm=llm or NullLeadLLM(), clock=lambda: NOW, result_sink=sink)


def _review(raw, records, evidence, llm=None, thread="t1"):
    sink: list = []
    deps = _deps(records, evidence, llm, sink.append)
    graph = build_graph(deps)
    cfg = {"configurable": {"thread_id": thread}}
    state = graph.invoke({"review_id": "rv_test", "raw_case": raw}, cfg)
    out = ReviewOutput.model_validate(state["review_output"])     # the contracts hold
    if out.checked_memo is not None:                              # memo.py's own object, unchanged
        CheckedMemo.model_validate(state["review_output"]["checked_memo"])
    return graph, cfg, state, out, sink, deps


def _status(out):
    return {c.check_id: c.status for c in out.lead.checks}


def test_case_1042_clears_after_internal_follow_ups(case_1042):
    graph, cfg, state, out, sink, deps = _review(case_1042, records_1042, evidence_1042)
    memo, lead = out.memo, out.lead
    assert memo.recommendation == "recommend_clear"
    assert out.passed and out.checked_memo.check_result.passed
    assert _status(out)["EC1.dba_owner"] == "EXPLAINED"
    assert _status(out)["EC2.tin_match"] == "EXPLAINED"
    assert memo.auto_resolved == ["NAME_SIMILAR_MATCH"] and memo.out_of_scope == ["PRODUCT_CATEGORY_REVIEW"]
    assert all(f.status == "resolved" for f in memo.flags)
    # Each flag's checks are in the report.
    assert {f.flag: f.check_ids for f in lead.flag_checks}["TIN_MISMATCH"][0] == "EC2.tin_match"
    # Internal follow-ups only; never more than the round limit; no merchant round.
    assert lead.follow_ups and all(f.target == "records" for f in lead.follow_ups)
    assert max(f.round for f in lead.follow_ups) <= MAX_INTERNAL_ROUNDS
    # Follow-up tasks went only to the affected worker.
    assert len(deps.evidence_worker.calls) == 1
    assert all(c.follow_up_of == "case_1042-records-1" for c in deps.records_worker.calls[1:])
    # Every ID the memo cites is in the evidence index, built by code; the report's extra IDs too.
    assert set(out.checked_memo.evidence_index) == memo.cited_ids()
    assert set(out.report_evidence_index) == lead.cited_ids() - memo.cited_ids()
    assert lead.confidence == min((c.confidence for c in lead.checks if c.required),
                                  key=["low", "medium", "high"].index)

    # The analyst approves; the result goes back to the backend.
    final = graph.invoke(Command(resume={"decision": "approve", "note": "ok", "analyst_id": "u_7"}), cfg)
    assert final["status"] == "done"
    assert sink[0]["case_status"] == "approved"
    assert sink[0]["decision_record"]["overridden"] is False
    assert sink[0]["decision_record"]["analyst_id"] == "u_7"


def test_case_3001_ein_of_unrelated_company_escalates(case_3001):
    *_, out, sink, _ = _review(case_3001, records_3001, evidence_3001)
    assert out.memo.recommendation == "escalate"
    assert out.lead.check("EC2.tin_match").hard_fail_reason == "ein_belongs_to_other_entity"


def test_case_2001_sole_proprietor_clears_without_provisional(case_2001):
    *_, out, _, _ = _review(case_2001, records_2001, evidence_2001)
    assert out.memo.recommendation == "recommend_clear", _status(out)
    reg = out.lead.check("EC4.registry_presence")
    assert reg.status == "EXPLAINED" and reg.expected_absence
    assert out.lead.provisional is False                  # nothing to re-check for a sole proprietor
    assert [r.cause for r in out.memo.ruled_out] == ["The SSN belongs to someone other than the owner"]


def test_new_llc_gets_a_provisional_clear(case_3001):
    formed = (TODAY - timedelta(days=20)).isoformat()
    case_3001.update(flag_reasons=["ENTITY_NOT_FOUND"], documents=[
        {"id": "D1", "type": "articles", "extracted": {"legal_name": "Maple Ridge Coffee Roasters LLC",
                                                        "formation_date": formed}, "confidence": 0.95}])
    case_3001["application"]["formation_date"] = formed

    def records(task):
        return [registry_none("Maple Ridge Coffee Roasters LLC", state="OH"), kyc()]

    def evidence(task):
        if task.follow_up_of:
            return []
        return [document("D1", "articles", legal_name="Maple Ridge Coffee Roasters LLC", formation_date=formed),
                bank("MAPLE RIDGE COFFEE ROASTERS LLC"),
                website(legal_name="Maple Ridge Coffee Roasters")]

    *_, out, _, _ = _review(case_3001, records, evidence)
    assert out.memo.recommendation == "recommend_clear"
    assert out.lead.provisional and out.lead.recheck_targets == ["registry"]
    assert out.lead.recheck_after == TODAY + timedelta(days=30)


def test_conflict_that_never_resolves_ends_in_a_merchant_request(case_1042):
    def records(task):  # the IRS never matches, and nothing explains the DBA owner
        return records_1042(task) if task.follow_up_of is None else [
            tin_match("case_1042", "NORTHLINE BICYCLE WORKS LLC", "Mismatch")]

    graph, cfg, state, out, _, deps = _review(case_1042, records, evidence_1042)
    memo = out.memo
    assert memo.recommendation == "request_documents"
    assert state["internal_rounds"] <= MAX_INTERNAL_ROUNDS
    merchant = [f for f in out.lead.follow_ups if f.target == "merchant"]
    assert len(merchant) == 1
    assert {r.type for r in memo.document_request} >= {"dba_certificate", "irs_147c"}
    # The report says which check each requested document would settle.
    assert {d.type: d.check_id for d in out.lead.document_checks}["dba_certificate"] == "EC1.dba_owner"
    # ...and only open checks get a merchant request (never one that already passes).
    assert all(out.lead.check(d.check_id).status not in ("AGREE", "EXPLAINED") for d in out.lead.document_checks)


def test_out_of_scope_flags_only_go_to_the_human_queue(case_1042):
    case_1042["flag_reasons"] = ["OFAC_POTENTIAL_MATCH"]
    *_, out, _, deps = _review(case_1042, records_1042, evidence_1042)
    assert out.lead.route == "human_queue" and out.memo.recommendation == "insufficient_evidence"
    assert out.memo.out_of_scope == ["OFAC_POTENTIAL_MATCH"]
    assert deps.records_worker.calls == [] and deps.evidence_worker.calls == []


def test_no_flags_goes_to_the_human_queue_without_a_memo(case_1042):
    """memo.Memo must name a flag, so there is only the report, with the reason."""
    case_1042["flag_reasons"] = []
    graph, cfg, state, out, sink, deps = _review(case_1042, records_1042, evidence_1042)
    assert out.checked_memo is None and out.recommendation == "insufficient_evidence"
    assert out.lead.route == "human_queue" and out.lead.route_reason == "no flags on the case"
    final = graph.invoke(Command(resume={"decision": "escalate", "note": "", "analyst_id": "u_7"}), cfg)
    assert final["status"] == "done"
    assert sink[0]["decision_record"]["agent_recommendation"] == "insufficient_evidence"
    assert sink[0]["decision_record"]["overridden"] is False


def test_missing_inputs_go_to_needs_info(case_3001):
    case_3001["application"].update(tin_type=None, tin_last4=None)
    case_3001["documents"] = []
    *_, out, _, deps = _review(case_3001, records_3001, evidence_3001)
    assert out.lead.route == "needs_info" and out.memo.recommendation == "request_documents"
    assert out.memo.document_request[0].type == "irs_147c"
    assert out.lead.document_checks[0].check_id == "EC2.ein_letter_matches"
    assert deps.records_worker.calls == []


def test_only_auto_resolved_flags_skip_the_workers(case_1042):
    case_1042["flag_reasons"] = ["NAME_SIMILAR_MATCH"]
    *_, out, _, deps = _review(case_1042, records_1042, evidence_1042)
    assert out.memo.recommendation == "recommend_clear" and out.memo.auto_resolved == ["NAME_SIMILAR_MATCH"]
    assert deps.records_worker.calls == []


def test_worker_crash_becomes_a_gap_not_a_crash(case_1042):
    def boom(task):
        raise RuntimeError("registry down")
    *_, out, _, _ = _review(case_1042, boom, evidence_1042)
    assert out.memo.recommendation == "insufficient_evidence"   # don't ask the merchant to cover our failure
    assert out.memo.document_request == []
    assert "registry down" in out.memo.notes_for_analyst


class StubLLM(NullLeadLLM):
    """An LLM that adds a check and tries to explain conflicts with a made-up citation."""

    def plan_extras(self, summary, checks):
        return PlanExtras(added_checks=[AddedCheck(slug="second_dba", edge_case="EC-1",
                                                   description="The website names a second DBA",
                                                   evidence_focus=["dba"])])

    def explain(self, check, diff, findings):
        return ExplainOut(explained=True, explanation="Looks fine", cited_finding_keys=["nope:f1"])


def test_llm_cannot_explain_without_real_citations(case_1042):
    def records(task):  # no filing history: nothing really explains the conflicts
        return records_1042(task) if task.follow_up_of is None else []

    *_, out, _, _ = _review(case_1042, records, evidence_1042, llm=StubLLM())
    statuses = _status(out)
    assert statuses["EC1.dba_owner"] == "CONFLICT"
    assert "ADD.second_dba" in statuses
    assert out.lead.check("ADD.second_dba").required is False
    assert out.memo.recommendation != "recommend_clear"


# ---------- API ----------

ANALYST = {"X-User-Id": "u1", "X-User-Role": "analyst"}     # as the gateway forwards them


def _client(case):
    """The API with the case service faked: POST /reviews reads the case from it by case_id."""
    cases = FakeCaseService(case)
    deps = _deps(records_1042, evidence_1042, sink=cases.send_decision)
    return TestClient(create_app(deps=deps, case_client=cases, background=False)), deps, cases


def test_api_start_is_idempotent_and_decision_resumes(case_1042):
    client, deps, cases = _client(case_1042)
    r1 = client.post("/reviews", json={"case_id": "case_1042"}, headers=ANALYST)
    assert r1.status_code == 202 and r1.json()["created"] is True
    rid = r1.json()["review_id"]
    assert r1.json()["status"] == "awaiting_decision"
    r2 = client.post("/reviews", json={"case_id": "case_1042"}, headers=ANALYST)
    assert r2.json()["review_id"] == rid and r2.json()["created"] is False
    assert len(deps.evidence_worker.calls) == 1                   # no second run

    body = client.get(f"/reviews/{rid}").json()
    assert body["review_output"]["checked_memo"]["memo"]["recommendation"] == "recommend_clear"
    CheckedMemo.model_validate(body["review_output"]["checked_memo"])     # the review app's contract

    assert client.post(f"/reviews/{rid}/decision", json={"decision": "approve"}).status_code == 403
    assert client.post(f"/reviews/{rid}/decision", json={"decision": "approve"},
                       headers={"X-User-Id": "u1", "X-User-Role": "viewer"}).status_code == 403
    r = client.post(f"/reviews/{rid}/decision", json={"decision": "approve", "note": "fine"}, headers=ANALYST)
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert cases.saved[0]["case_status"] == "approved"
    again = client.post(f"/reviews/{rid}/decision", json={"decision": "deny"}, headers=ANALYST)
    assert again.status_code == 409


def test_api_same_case_with_new_evidence_starts_a_new_review(case_1042):
    client, _, _ = _client(case_1042)       # the fake case service holds this same dict
    rid = client.post("/reviews", json={"case_id": "case_1042"}, headers=ANALYST).json()["review_id"]
    case_1042["documents"].append({"id": "D2", "type": "irs_147c",
                                   "extracted": {"name": "NORTHLINE BICYCLE WORKS LLC", "ein_last4": "4567"},
                                   "confidence": 0.9})
    rid2 = client.post("/reviews", json={"case_id": "case_1042"}, headers=ANALYST).json()["review_id"]
    assert rid2 != rid


def test_api_rejects_a_payload_without_a_case_id(case_1042):
    client, _, _ = _client(case_1042)
    for body in ({}, {"case_id": "1042"}, {"case": {"flag_reasons": ["TIN_MISMATCH"]}}):
        assert client.post("/reviews", json=body, headers=ANALYST).status_code == 422
    assert client.get("/reviews/rv_nope").status_code == 404


def test_a_crashed_run_releases_its_idempotency_key(case_1042):
    from app.api.service import ReviewService

    class FlakyGraph:
        """Crashes on the first run (e.g. the checkpointer is down), works afterwards."""
        def __init__(self):
            self.runs = 0
            self.real = build_graph(_deps(records_1042, evidence_1042))

        def invoke(self, inp, cfg):
            self.runs += 1
            if self.runs == 1:
                raise RuntimeError("checkpointer unavailable")
            return self.real.invoke(inp, cfg)

        def get_state(self, cfg):
            return self.real.get_state(cfg)

    graph = FlakyGraph()
    service = ReviewService(graph)
    rid1, created1 = service.start(case_1042, background=False)
    assert created1 and service.status(rid1)["status"] == "failed"
    rid2, created2 = service.start(case_1042, background=False)          # the backend retries
    assert created2 and rid2 != rid1 and service.status(rid2)["status"] == "awaiting_decision"
    rid3, created3 = service.start(case_1042, background=False)          # a retry of a good run doesn't re-run
    assert (rid3, created3) == (rid2, False) and graph.runs == 2
