"""The orchestrator is a lookup table: routing, input checks and idempotency are plain code."""

from kyb_contracts.case_input import FlagReason
from kyb_contracts.memo import FLAG_EDGE_CASE

from app.graph.orchestrator import (
    OUT_OF_SCOPE,
    ROUTING_TABLE,
    IdempotencyStore,
    idempotency_key,
    route_case,
)


def test_every_in_scope_flag_has_a_route_and_out_of_scope_flags_dont():
    assert set(ROUTING_TABLE) == set(FLAG_EDGE_CASE)
    assert OUT_OF_SCOPE == {FlagReason.PRODUCT_CATEGORY_REVIEW, FlagReason.OFAC_POTENTIAL_MATCH}


def test_case_1042_routes_ec1_and_ec2(case_1042):
    d, case = route_case(case_1042)
    assert d.route == "reviewed" and case is not None
    assert d.open_flags == [FlagReason.DBA_UNVERIFIED, FlagReason.TIN_MISMATCH]
    assert [a.flag for a in d.auto_resolved] == [FlagReason.NAME_SIMILAR_MATCH]   # L.L.C. vs LLC only
    assert d.out_of_scope == [FlagReason.PRODUCT_CATEGORY_REVIEW]
    assert d.edge_cases == ["EC-1", "EC-2"]
    assert d.workers == ["records", "evidence"]


def test_combined_flags_merge_and_dedupe_tasks(case_1042):
    d, _ = route_case(case_1042)
    # EC-1 and EC-2 both ask for the registry entity and filing history: looked up once.
    assert d.records_tasks.count("registry_by_entity_id") == 1
    assert d.records_tasks.count("filing_history") == 1
    assert len(d.records_tasks) == len(set(d.records_tasks))
    assert {"dba_filing", "tin_match", "name_control"} <= set(d.records_tasks)
    assert {"ein_letter", "bank_account", "website"} <= set(d.evidence_tasks)


def test_similar_name_that_really_differs_stays_open(case_1042):
    case_1042["checks"]["sos_name"]["matched_name"] = "NORTHLINE BIKE WORKS LLC"
    d, _ = route_case(case_1042)
    assert FlagReason.NAME_SIMILAR_MATCH in d.open_flags


def test_no_flags_goes_to_human_queue(case_1042):
    case_1042["flag_reasons"] = []
    d, case = route_case(case_1042)
    assert (d.route, d.reason, case) == ("human_queue", "no flags on the case", None)


def test_unknown_flag_goes_to_human_queue(case_1042):
    case_1042["flag_reasons"].append("SOMETHING_NEW")
    d, _ = route_case(case_1042)
    assert d.route == "human_queue" and "SOMETHING_NEW" in d.reason


def test_malformed_payload_goes_to_human_queue(case_1042):
    del case_1042["application"]["state"]
    d, case = route_case(case_1042)
    assert d.route == "human_queue" and d.reason.startswith("malformed payload") and case is None
    d, _ = route_case("not a dict")
    assert d.route == "human_queue" and d.case_id is None


def test_only_out_of_scope_flags_go_to_human_queue(case_1042):
    case_1042["flag_reasons"] = ["OFAC_POTENTIAL_MATCH"]
    d, case = route_case(case_1042)
    assert d.route == "human_queue" and d.out_of_scope == [FlagReason.OFAC_POTENTIAL_MATCH]
    assert case is not None


def test_ec2_without_tin_or_ein_letter_is_rejected_before_any_agent(case_3001):
    case_3001["application"]["tin_type"] = None
    case_3001["application"]["tin_last4"] = None
    case_3001["documents"] = []
    d, _ = route_case(case_3001)
    assert d.route == "needs_info"
    assert d.missing_inputs[0]["document_type"] == "irs_147c"
    assert d.records_tasks == [] and d.evidence_tasks == []    # no worker runs


def test_ec2_with_only_the_letter_is_fine(case_3001):
    case_3001["application"]["tin_type"] = None
    case_3001["application"]["tin_last4"] = None
    d, _ = route_case(case_3001)
    assert d.route == "reviewed"


def test_idempotency_key_is_case_id_plus_input_hash(case_1042):
    k1 = idempotency_key(case_1042)
    assert k1.startswith("case_1042:")
    assert idempotency_key(dict(reversed(list(case_1042.items())))) == k1    # key order doesn't matter
    case_1042["documents"].append({"id": "D2", "type": "irs_147c", "extracted": {"name": "X"}, "confidence": 0.9})
    assert idempotency_key(case_1042) != k1     # same case_id back with new evidence: new run


def test_idempotency_store_returns_the_first_review():
    store = IdempotencyStore()
    assert store.get_or_claim("case_1:abc", "rv_1") == ("rv_1", True)
    assert store.get_or_claim("case_1:abc", "rv_2") == ("rv_1", False)
    assert store.get_or_claim("case_1:def", "rv_3") == ("rv_3", True)
