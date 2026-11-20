"""Checks that keep the Lead and the workers in step.

- Field names: the tools write only ``FINDING_FIELDS`` (enforced in ``ToolContext.add_finding``),
  every fact the Lead reads is written by some tool, and the Lead's focus fields are
  translated into the names the tools write.
- R IDs: one tool call adds at most ``MAX_RECORDS_PER_CALL`` R sources, so parallel Records
  follow-ups, each given ``R_ID_STRIDE`` R numbers, can never hand out the same R ID twice.
"""

from __future__ import annotations

import pytest

from helpers import tool_context

from kyb_contracts.worker_io import FINDING_FIELDS

from app.graph.facts import EVIDENCE_FIELDS, RECORDS_FIELDS, worker_fields
from app.graph.followup import FOLLOW_UP_TOOL_CALLS, PLAYBOOK, R_ID_STRIDE
from app.graph.plan import TEMPLATES
from app.tools import RECORDS_TOOLS
from app.tools._common import MAX_RECORDS_PER_CALL

TABLES = {"records": RECORDS_FIELDS, "evidence": EVIDENCE_FIELDS}

# Facts the Lead reads that no tool writes, and why. Both are by design. Any other fact
# missing from the tools is a check that can never pass, and fails this test.
NOT_WRITTEN_BY_TOOLS = {
    ("records", "separate_entity"): "derived by FactIndex from other entities' legal_name",
    ("records", "name_control"): "the IRS name is never revealed; name_control_tested is written instead",
}


# ---------------------------------------------------------------- field names

def test_every_fact_the_lead_reads_is_written_by_a_tool_or_explained():
    unwritten = {(worker, fact) for worker, table in TABLES.items()
                 for fact, accepted in table.items() if not accepted & FINDING_FIELDS[worker]}
    assert unwritten == set(NOT_WRITTEN_BY_TOOLS)


def test_the_tools_refuse_a_field_outside_the_contract():
    ctx = tool_context("case_1042", "records")
    source = RECORDS_TOOLS["get_registry_entity"].run(ctx, {}).sources[0]
    excerpt = ctx.rendered[source.source_id].splitlines()[-1]
    with pytest.raises(AssertionError, match="FINDING_FIELDS"):
        ctx.add_finding(source, "irs_name", "SECRET", excerpt, method="structured_record", confidence="high")


def _focus_names():
    for t in TEMPLATES:
        yield from (("records", n) for n in t.records_focus)
        yield from (("evidence", n) for n in t.evidence_focus)
    for steps, _ in PLAYBOOK.values():
        for step in steps:
            yield from ((step.target, n) for n in step.focus)


def _unwritten_fact(worker: str, name: str) -> bool:
    return any((worker, fact) in NOT_WRITTEN_BY_TOOLS
               for fact, accepted in TABLES[worker].items() if name == fact or name in accepted)


@pytest.mark.parametrize("worker, name", sorted(set(_focus_names())))
def test_every_focus_name_reaches_a_tool_field(worker, name):
    fields = worker_fields(worker, [name])
    assert set(fields) <= FINDING_FIELDS[worker]
    assert fields or _unwritten_fact(worker, name), f"{worker} focus {name!r} matches no field a tool writes"


def test_worker_fields_translates_lead_names_to_tool_names():
    assert worker_fields("records", ["tin_match", "dba_owner", "irs_name_control", "legal_name"]) == [
        "tin_match_result", "registrant", "legal_name"]
    assert worker_fields("evidence", ["license_status", "holder", "holder"]) == ["status", "holder"]


def test_follow_up_prior_findings_use_tool_field_names(case_1042):
    """A follow-up about 'dba_owner' and 'tin_match' gets back the worker's 'registrant' and
    'tin_match_result' findings (it used to compare the Lead's names with the tools' names)."""
    from datetime import datetime

    from conftest import TODAY
    from fakes import ScriptedWorker, records_1042

    from app.graph.facts import FactIndex
    from app.graph.followup import PendingFollowUp, follow_up_task
    from app.graph.orchestrator import route_case
    from app.graph.plan import build_plan, initial_tasks

    routing, case = route_case(case_1042)
    plan = build_plan(case, routing, TODAY)
    first = next(t for t in initial_tasks(case, routing, plan) if t.worker == "records")
    results = [ScriptedWorker("records", records_1042).run(first)]
    fu = PendingFollowUp(follow_up_id="fu1", check_id="EC1.dba_owner", target="records", round=1,
                         ask="Who owns the DBA?", reason="conflict", task_id="case_1042-records-fu1",
                         status_before="CONFLICT", sent_at=datetime(2026, 11, 19),
                         focus=("dba_owner", "dba_name", "tin_match"))
    follow_up = follow_up_task(case, plan, fu, results, FactIndex(case, [(0, r) for r in results]),
                               {"records": first.task_id}, slot=0)
    assert follow_up.focus_fields == ["registrant", "dba", "tin_match_result"]
    assert {f.field for f in follow_up.prior_findings} == {"registrant", "dba", "tin_match_result"}


# ---------------------------------------------------------------- R IDs

class ManyResults:
    """The fixtures' client, but every search returns 10 matching results."""

    def __init__(self, base):
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    def search_registry(self, state, name=None, officer=None, address=None):
        entity = self.base.get_registry_entity("IL", "IL-0458821")
        return [entity.model_copy(update={"state_entity_id": f"IL-99{i:05d}"}) for i in range(10)]

    def search_dba(self, state, name, county=None):
        filing = self.base.search_dba("IL", "Northline Cycles")[0]
        return [filing.model_copy(update={"filing_id": f"DBA-{i}", "status": "expired" if i % 2 else "active"})
                for i in range(10)]

    def search_commercial(self, state, name=None, address=None):
        profile = self.base.search_commercial("IL", name="Northline Bicycle Works")[0]
        return [profile.model_copy(update={"profile_id": f"CDB-{i}"}) for i in range(10)]


@pytest.mark.parametrize("tool, args", [
    ("search_registry", {"name": "Northline Bicycle Works"}),
    ("search_dba", {"name": "Northline Cycles"}),
    ("search_commercial_db", {"name": "Northline Bicycle Works"}),
])
def test_one_search_adds_at_most_max_records_per_call(tool, args):
    from helpers import fake_client

    ctx = tool_context("case_1042", "records", client=ManyResults(fake_client()))
    out = RECORDS_TOOLS[tool].run(ctx, args)
    r_ids = [s.source_id for s in out.sources if s.source_id.startswith("R")]
    assert 0 < len(r_ids) <= MAX_RECORDS_PER_CALL
    if tool == "search_dba":
        assert "showing the 3 closest" in out.note
        statuses = [f.value for f in out.findings if f.field == "dba_status"]
        assert statuses == ["active"] * len(statuses)


def test_parallel_follow_ups_cannot_share_r_ids():
    assert FOLLOW_UP_TOOL_CALLS * MAX_RECORDS_PER_CALL <= R_ID_STRIDE
