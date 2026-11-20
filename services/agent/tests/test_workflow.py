"""The whole graph, end to end, with scripted workers and no LLM (or a stub LLM)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from langgraph.types import Command

from conftest import NOW, TODAY
from fakes import (
    Rec,
    ScriptedWorker,
    evidence_1042,
    evidence_2001,
    evidence_3001,
    kyc,
    records_1042,
    records_2001,
    records_3001,
)
from kyb_contracts.memo import ReviewOutput

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
    out = ReviewOutput.model_validate(state["review_output"])     # the contract holds
    return graph, cfg, state, out, sink, deps


def test_case_1042_clears_after_internal_follow_ups(case_1042):
    graph, cfg, state, out, sink, deps = _review(case_1042, records_1042, evidence_1042)
    memo = out.memo
    assert memo.recommendation == "recommend_clear"
    assert out.check_result.passed
    assert {c.check_id: c.status for c in memo.checks}["EC1.dba_owner"] == "EXPLAINED"
    assert {c.check_id: c.status for c in memo.checks}["EC2.tin_match"] == "EXPLAINED"
    assert memo.auto_resolved == ["NAME_SIMILAR_MATCH"] and memo.out_of_scope == ["PRODUCT_CATEGORY_REVIEW"]
    assert all(f.status == "resolved" for f in memo.flags)
    # Internal follow-ups only; never more than the round limit; no merchant round.
    assert memo.follow_ups and all(f.target == "records" for f in memo.follow_ups)
    assert max(f.round for f in memo.follow_ups) <= MAX_INTERNAL_ROUNDS
    # Follow-up tasks went only to the affected worker.
    assert len(deps.evidence_worker.calls) == 1
    assert all(c.follow_up_of == "case_1042-records-1" for c in deps.records_worker.calls[1:])
    # Every cited ID is in the evidence index, built by code.
    assert set(out.evidence_index) == memo.cited_ids()
    assert memo.confidence == min((c.confidence for c in memo.checks if c.required),
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
    tin = next(c for c in out.memo.checks if c.check_id == "EC2.tin_match")
    assert tin.hard_fail_reason == "ein_belongs_to_other_entity"


def test_case_2001_sole_proprietor_clears_without_provisional(case_2001):
    *_, out, _, _ = _review(case_2001, records_2001, evidence_2001)
    memo = out.memo
    assert memo.recommendation == "recommend_clear", [(c.check_id, c.status) for c in memo.checks]
    reg = next(c for c in memo.checks if c.check_id == "EC4.registry_presence")
    assert reg.status == "EXPLAINED" and reg.expected_absence
    assert memo.provisional is False                      # nothing to re-check for a sole proprietor


def test_new_llc_gets_a_provisional_clear(case_3001):
    formed = (TODAY - timedelta(days=20)).isoformat()
    case_3001.update(flag_reasons=["ENTITY_NOT_FOUND"], documents=[
        {"id": "D1", "type": "articles", "extracted": {"legal_name": "Maple Ridge Coffee Roasters LLC",
                                                        "formation_date": formed}, "confidence": 0.95}])
    case_3001["application"]["formation_date"] = formed

    def records(task):
        return [Rec("sos_registry", [("registry_found", False, "no OH entity yet")]), kyc()]

    def evidence(task):
        if task.follow_up_of:
            return []
        return [Rec("articles", [("formation_date", formed, f"filed {formed}")], source_id="D1", authority="merchant"),
                Rec("bank_verification", [("account_holder", "MAPLE RIDGE COFFEE ROASTERS LLC", "h"),
                                          ("bank_status", "verified", "v")], source_id="B1", authority="third_party"),
                Rec("website", [("legal_name", "Maple Ridge Coffee Roasters", "n")], source_id="W1",
                    authority="merchant", confidence="low")]

    *_, out, _, _ = _review(case_3001, records, evidence)
    memo = out.memo
    assert memo.recommendation == "recommend_clear"
    assert memo.provisional and memo.recheck_targets == ["registry"]
    assert memo.recheck_after == TODAY + timedelta(days=30)


def test_conflict_that_never_resolves_ends_in_a_merchant_request(case_1042):
    def records(task):  # the IRS never matches, and nothing explains the DBA owner
        return records_1042(task) if task.follow_up_of is None else [
            Rec("irs_tin_match", [("tin_match", "3", "code 3 ('NORTHLINE BICYCLE WORKS LLC')")])]

    graph, cfg, state, out, _, deps = _review(case_1042, records, evidence_1042)
    memo = out.memo
    assert memo.recommendation == "request_documents"
    assert state["internal_rounds"] <= MAX_INTERNAL_ROUNDS
    merchant = [f for f in memo.follow_ups if f.target == "merchant"]
    assert len(merchant) == 1
    assert {r.document_type for r in memo.document_request} >= {"dba_certificate", "irs_147c"}


def test_no_flags_goes_to_the_human_queue(case_1042):
    case_1042["flag_reasons"] = ["OFAC_POTENTIAL_MATCH"]
    *_, out, _, deps = _review(case_1042, records_1042, evidence_1042)
    assert out.memo.route == "human_queue" and out.memo.recommendation == "insufficient_evidence"
    assert deps.records_worker.calls == [] and deps.evidence_worker.calls == []


def test_missing_inputs_go_to_needs_info(case_3001):
    case_3001["application"].update(tin_type=None, tin_last4=None)
    case_3001["documents"] = []
    *_, out, _, deps = _review(case_3001, records_3001, evidence_3001)
    assert out.memo.route == "needs_info" and out.memo.recommendation == "request_documents"
    assert out.memo.document_request[0].document_type == "irs_147c"
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
    statuses = {c.check_id: c.status for c in out.memo.checks}
    assert statuses["EC1.dba_owner"] == "CONFLICT"
    assert "ADD.second_dba" in statuses
    assert next(c for c in out.memo.checks if c.check_id == "ADD.second_dba").required is False
    assert out.memo.recommendation != "recommend_clear"


# ---------- API ----------

def _client(sink=None):
    deps = _deps(records_1042, evidence_1042, sink=sink)
    return TestClient(create_app(deps=deps, case_client=object(), background=False)), deps


def test_api_start_is_idempotent_and_decision_resumes(case_1042):
    sink: list = []
    client, deps = _client(sink.append)
    r1 = client.post("/reviews", json={"case": case_1042})
    assert r1.status_code == 202 and r1.json()["created"] is True
    rid = r1.json()["review_id"]
    assert r1.json()["status"] == "awaiting_decision"
    r2 = client.post("/reviews", json={"case": case_1042})
    assert r2.json()["review_id"] == rid and r2.json()["created"] is False
    assert len(deps.evidence_worker.calls) == 1                   # no second run

    body = client.get(f"/reviews/{rid}").json()
    assert body["review_output"]["memo"]["recommendation"] == "recommend_clear"

    assert client.post(f"/reviews/{rid}/decision", json={"decision": "approve"}).status_code == 403
    assert client.post(f"/reviews/{rid}/decision", json={"decision": "approve"},
                       headers={"X-User-Id": "u1", "X-User-Role": "viewer"}).status_code == 403
    r = client.post(f"/reviews/{rid}/decision", json={"decision": "approve", "note": "fine"},
                    headers={"X-User-Id": "u1", "X-User-Role": "analyst"})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert sink[0]["case_status"] == "approved"
    again = client.post(f"/reviews/{rid}/decision", json={"decision": "deny"},
                        headers={"X-User-Id": "u1", "X-User-Role": "analyst"})
    assert again.status_code == 409


def test_api_same_case_with_new_evidence_starts_a_new_review(case_1042):
    client, _ = _client()
    rid = client.post("/reviews", json={"case": case_1042}).json()["review_id"]
    case_1042["documents"].append({"id": "D2", "type": "irs_147c",
                                   "extracted": {"name": "NORTHLINE BICYCLE WORKS LLC", "ein_last4": "4567"},
                                   "confidence": 0.9})
    rid2 = client.post("/reviews", json={"case": case_1042}).json()["review_id"]
    assert rid2 != rid


def test_api_rejects_a_payload_without_a_case_id():
    client, _ = _client()
    assert client.post("/reviews", json={"case": {"flag_reasons": ["TIN_MISMATCH"]}}).status_code == 422
    assert client.get("/reviews/rv_nope").status_code == 404
