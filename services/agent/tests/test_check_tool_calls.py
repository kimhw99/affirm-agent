"""scripts/check_tool_calls.py: the workers' recorded tool paths against expected.json's tool_calls.

The paths come from the real workers and tools (with the scripted model of
test_workers.py), recorded with record_tool_paths() as an evaluation would, so a change
to what the workers publish breaks these tests too.
"""

import importlib.util
import json
import sys
from pathlib import Path

from helpers import NOW, fake_client, task
from test_workers import ScriptedLLM, calls

from kyb_contracts.golden import ExpectedAnswer, ToolCalls

from app.workers import record_tool_paths, run_evidence_worker, run_records_worker

REPO = Path(__file__).resolve().parents[3]
CASE_1042 = REPO / "data" / "golden" / "combo" / "case_1042"

_spec = importlib.util.spec_from_file_location("check_tool_calls", REPO / "scripts" / "check_tool_calls.py")
check_tool_calls = importlib.util.module_from_spec(_spec)
sys.modules["check_tool_calls"] = check_tool_calls
_spec.loader.exec_module(check_tool_calls)

EXPECTED = ExpectedAnswer.model_validate_json((CASE_1042 / "expected.json").read_text(encoding="utf-8"))


def records(steps, **changes):
    return run_records_worker(task("case_1042", "records", **changes), fake_client(), llm=ScriptedLLM(steps), now=NOW)


def evidence(steps):
    return run_evidence_worker(task("case_1042", "evidence"), fake_client(), llm=ScriptedLLM(steps), now=NOW)


def evidence_pass():
    return evidence([calls(("get_document", {"doc_id": "D1"}), ("get_bank_verification", {"bank_id": "B1"}),
                           ("get_site_identity", {"web_id": "W1"}))])


def ideal_review():
    """(results, paths) of a review that does what case_1042 needs, in its first tasks."""
    with record_tool_paths() as paths:
        results = [
            records([
                calls(("get_registry_entity", {}), ("search_dba", {"name": "Northline Cycles"}),
                      ("check_tin_match", {"name": "Northline Bicycle Works LLC"}), ("get_kyc_result", {})),
                calls(("get_filing_history", {}), ("check_tin_match", {"name": "HARBOR LIGHT CYCLES LLC"})),
            ]),
            evidence_pass(),
        ]
    return results, paths


def old_review():
    """What the live trace showed before BEAC-30: a thin first pass, then two overlapping follow-ups."""
    follow_up = dict(follow_up_of="t1", max_tool_calls=4)
    with record_tool_paths() as paths:
        results = [
            records([calls(("get_registry_entity", {}), ("search_dba", {"name": "Northline Cycles"}))],
                    follow_up_of="t0"),   # no checklist reminders, like the old worker
            records([calls(("check_tin_match", {"name": "HARBOR LIGHT CYCLES LLC"}),
                           ("check_tin_match", {"name": "Northline Bicycle Works LLC"}),
                           ("check_tin_match", {"name": "Northline Cycles"}),
                           ("check_tin_match", {"name": "HARBOR LIGHT CYCLES"}))],
                    task_id="case_1042-records-F1", record_id_start=3, **follow_up),
            records([calls(("check_tin_match", {"name": "HARBOR LIGHT CYCLES LLC"}),
                           ("check_tin_match", {"name": "NORTHLINE BICYCLE WORKS, L.L.C."}),
                           ("get_kyc_result", {}))],
                    task_id="case_1042-records-F2", record_id_start=23, **follow_up),
            evidence_pass(),
        ]
    return results, paths


def report(reports, worker):
    return next(r for r in reports if r.worker == worker)


def test_the_ideal_path_passes_case_1042():
    reports = check_tool_calls.check(EXPECTED.tool_calls, *ideal_review())
    assert [(r.worker, r.failures, r.tool_calls_used, r.repeats) for r in reports] == [
        ("records", [], 6, 0), ("evidence", [], 3, 0)]


def test_the_old_path_fails_where_the_live_trace_did():
    rec = report(check_tool_calls.check(EXPECTED.tool_calls, *old_review()), "records")
    assert rec.tool_calls_used == 9
    # Within F1, HARBOR LIGHT CYCLES and Northline Cycles repeat a name control; F2 repeats two from F1.
    assert rec.repeats == 4
    assert rec.failures == [
        "never called: get_filing_history -> sos_filing_history where former_name=HARBOR LIGHT CYCLES LLC",
        "9 tool calls, more than the 8 expected",
        "4 repeated call(s), more than the 0 allowed",
    ]


def test_a_step_left_to_a_follow_up_still_counts():
    # The TIN match on the old name came only in F1, but it came: no failure for it.
    rec = report(check_tool_calls.check(EXPECTED.tool_calls, *old_review()), "records")
    assert not any("check_tin_match" in f for f in rec.failures)


def test_a_call_that_shouldnt_happen_is_reported():
    expected = ToolCalls.model_validate({"records": {"must_not_call": [
        {"tool": "check_tin_match", "where": {"name_control_tested": "NORT", "tin_match_result": "Match"}},
        {"tool": "search_dba", "source": "state_dba"},
    ]}})
    rec = report(check_tool_calls.check(expected, *ideal_review()), "records")
    assert rec.failures == ["called, but shouldn't be: search_dba -> state_dba (t1 turn 1)"]


def test_a_rejected_call_matches_nothing():
    with record_tool_paths() as paths:
        results = [records([calls(("get_kyc_result", {"owner_ref": "P1"}))], follow_up_of="t0")]
    expected = ToolCalls.model_validate({"records": {"must_call": [{"tool": "get_kyc_result", "source": "K1"}]}})
    assert report(check_tool_calls.check(expected, results, paths), "records").failures == [
        "never called: get_kyc_result -> K1"]


def test_a_path_without_its_result_is_reported():
    results, paths = ideal_review()
    rec = report(check_tool_calls.check(EXPECTED.tool_calls, [r for r in results if r.worker != "records"], paths),
                 "records")
    assert rec.failures[0] == "no WorkerResult for t1, so its calls can't be matched by source"


def _write_run(path: Path, results, paths, wrap=False) -> None:
    dumped = [r.model_dump(mode="json") for r in results]
    path.write_text(json.dumps({
        "worker_results": [{"round": 0, "result": r} for r in dumped] if wrap else dumped,
        "tool_paths": [p.model_dump(mode="json") for p in paths],
    }))


def test_the_command_line_reads_a_run(tmp_path, capsys):
    run = tmp_path / "run.json"
    _write_run(run, *ideal_review(), wrap=True)   # the Lead's worker_results, with their rounds
    assert check_tool_calls.main([str(CASE_1042), str(run)]) == 0
    out = capsys.readouterr().out
    assert "t1 turn 2: get_filing_history() -> R4=sos_filing_history" in out
    assert "case_1042: 0 failure(s)." in out

    _write_run(run, *old_review())
    assert check_tool_calls.main([str(CASE_1042), str(run)]) == 1
    assert "FAIL never called: get_filing_history" in capsys.readouterr().out


def test_a_case_without_tool_calls_is_a_usage_error(tmp_path, capsys):
    folder = tmp_path / "case_1042"
    folder.mkdir()
    answer = json.loads((CASE_1042 / "expected.json").read_text(encoding="utf-8"))
    del answer["tool_calls"]
    (folder / "expected.json").write_text(json.dumps(answer))
    (tmp_path / "run.json").write_text("{}")
    assert check_tool_calls.main([str(folder), str(tmp_path / "run.json")]) == 2
    assert "no tool_calls to check" in capsys.readouterr().out
