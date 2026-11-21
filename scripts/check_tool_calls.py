"""Checks the workers' tool calls in one review against a golden case's expected tool path.

The Lead never sees which tools a worker used, so the tool path isn't in WorkerResult.
Each worker task publishes a ToolPath instead (contracts/kyb_contracts/tool_path.py): the
tool, the model's arguments, the sources it returned and whether it ran, repeated an
earlier call, was rejected, failed, or was over the budget. An evaluation records them
with ``record_tool_paths()`` (services/agent/app/workers/tool_path.py). This script joins
each ToolPath to its WorkerResult by task_id, to see the source types and findings of
what a call returned, and checks the calls against the golden case's expected.json
(``tool_calls``, golden.py):

- must_call: each item matched by some call: the tool, and the source it returned (an R
  source type, or D1, B1, W1, K1), with the finding values in ``where``. A repeat counts.
- must_not_call: no call matches, e.g. a registry lookup of a look-alike company.
- max_calls: the calls counted in tool_calls_used, over the first task and every follow-up.
- max_repeats: calls that fetched what an earlier call in the review already had: the same
  sources again, or, for check_tin_match, the same IRS name control.

It reads every task of the review, so a step left to a follow-up still counts. What it
can't judge, such as whether the order of the calls made sense, is for a reviewer reading
the path it prints.

Usage, from the repo root:

    python scripts/check_tool_calls.py data/golden/combo/case_1042 run.json

run.json holds {"worker_results": [...], "tool_paths": [...]}: the review's WorkerResults
(or the Lead's worker_results, {"round": n, "result": {...}}) and the ToolPaths recorded
during it. It prints each worker's path and every failure, and exits with 1 if there are
failures, 0 if there are none, and 2 if the case has no expected tool calls or a file
can't be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "contracts"))  # use this repo's contracts, installed or not

from kyb_contracts.golden import TOOL_ID_PREFIX, ExpectedAnswer, ToolCallRef, ToolCalls  # noqa: E402
from kyb_contracts.tool_path import ToolCall, ToolPath  # noqa: E402
from kyb_contracts.worker_io import Finding, Source, WorkerResult  # noqa: E402

RAN = ("ok", "repeat")   # calls that returned sources


@dataclass
class Call:
    """One tool call with what it returned, resolved in its task's WorkerResult."""

    task_id: str
    call: ToolCall
    sources: list[Source]
    findings: dict[str, list[Finding]]   # source_id -> that source's findings

    def line(self) -> str:
        made = ", ".join(f"{s.source_id}={s.source_type}" for s in self.sources) or "-"
        status = "" if self.call.status == "ok" else f" [{self.call.status}]"
        args = json.dumps(self.call.args, ensure_ascii=False) if self.call.args else ""
        return f"{self.task_id} turn {self.call.turn}: {self.call.tool}({args}) -> {made}{status}"


@dataclass
class WorkerReport:
    worker: str
    calls: list[Call] = field(default_factory=list)
    tool_calls_used: int = 0
    repeats: int = 0
    failures: list[str] = field(default_factory=list)


def resolve(results: Iterable[WorkerResult], paths: Iterable[ToolPath], worker: str) -> tuple[list[Call], int, list[str]]:
    """The worker's calls over the review in order, its total tool_calls_used, and the tasks
    whose WorkerResult is missing (their calls can't be matched by source)."""
    by_task = {r.task_id: r for r in results if r.worker == worker}
    calls: list[Call] = []
    used = 0
    unmatched: list[str] = []
    for path in paths:
        if path.worker != worker:
            continue
        used += path.tool_calls_used
        result = by_task.get(path.task_id)
        if result is None:
            unmatched.append(path.task_id)
        sources = {s.source_id: s for s in result.sources} if result else {}
        findings: dict[str, list[Finding]] = {}
        for f in result.findings if result else []:
            findings.setdefault(f.source_id, []).append(f)
        for call in path.tool_calls:
            calls.append(Call(path.task_id, call, [sources[s] for s in call.source_ids if s in sources], findings))
    return calls, used, unmatched


def matches(ref: ToolCallRef, call: Call) -> bool:
    """The call is ref's tool, ran, and returned a source that is ref's source and has its findings."""
    if call.call.tool != ref.tool or call.call.status not in RAN:
        return False
    sources = call.sources
    if ref.source:
        by_id = ref.tool in TOOL_ID_PREFIX
        sources = [s for s in sources if (s.source_id if by_id else s.source_type) == ref.source]
        if not sources:
            return False
    if not ref.where:
        return True
    return any(all(any(f.field == k and f.value == v for f in call.findings.get(s.source_id, []))
                   for k, v in ref.where.items())
               for s in sources)


def _key(call: Call) -> tuple | None:
    """What a call fetched, to spot a repeat: its sources, or for a TIN check, the name control."""
    if call.call.status not in RAN or not call.sources:
        return None
    if call.call.tool == "check_tin_match":
        controls = [f.value for s in call.sources for f in call.findings.get(s.source_id, [])
                    if f.field == "name_control_tested"]
        if controls:
            return ("check_tin_match", str(controls[0]))
    return (call.call.tool, frozenset((s.source_type, s.locator) for s in call.sources))


def count_repeats(calls: list[Call]) -> int:
    seen: set[tuple] = set()
    repeats = 0
    for call in calls:
        key = _key(call)
        if key is None:
            continue
        if key in seen:
            repeats += 1
        seen.add(key)
    return repeats


def check(expected: ToolCalls, results: Iterable[WorkerResult], paths: Iterable[ToolPath]) -> list[WorkerReport]:
    """One report per worker the expectation covers, with its path, counts and failures."""
    results, paths = list(results), list(paths)
    reports = []
    for worker in ("records", "evidence"):
        rule = getattr(expected, worker)
        if rule is None:
            continue
        calls, used, unmatched = resolve(results, paths, worker)
        report = WorkerReport(worker, calls, used, count_repeats(calls))
        report.failures += [f"no WorkerResult for {task_id}, so its calls can't be matched by source"
                            for task_id in unmatched]
        for ref in rule.must_call:
            if not any(matches(ref, c) for c in calls):
                report.failures.append(f"never called: {ref.describe()}")
        for ref in rule.must_not_call:
            hits = [c for c in calls if matches(ref, c)]
            if hits:
                report.failures.append(f"called, but shouldn't be: {ref.describe()} ({hits[0].task_id} "
                                       f"turn {hits[0].call.turn})")
        if rule.max_calls is not None and used > rule.max_calls:
            report.failures.append(f"{used} tool calls, more than the {rule.max_calls} expected")
        if rule.max_repeats is not None and report.repeats > rule.max_repeats:
            report.failures.append(f"{report.repeats} repeated call(s), more than the {rule.max_repeats} allowed")
        reports.append(report)
    return reports


def load_run(data: dict[str, Any]) -> tuple[list[WorkerResult], list[ToolPath]]:
    """{"worker_results": [...], "tool_paths": [...]}; a result may be wrapped as {"round": n, "result": ...}."""
    results = [WorkerResult.model_validate(item["result"] if "result" in item and "round" in item else item)
               for item in data.get("worker_results", [])]
    return results, [ToolPath.model_validate(p) for p in data.get("tool_paths", [])]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a review's tool calls against a golden case's expected path.")
    parser.add_argument("case", type=Path, help="the golden case folder, with its expected.json")
    parser.add_argument("run", type=Path, help='a JSON file: {"worker_results": [...], "tool_paths": [...]}')
    args = parser.parse_args(argv)
    try:
        expected = ExpectedAnswer.model_validate_json((args.case / "expected.json").read_text(encoding="utf-8"))
        results, paths = load_run(json.loads(args.run.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError) as error:   # pydantic's ValidationError is a ValueError
        print(f"Can't read the files: {type(error).__name__}: {str(error).splitlines()[0]}")
        return 2
    if expected.tool_calls is None:
        print(f"{expected.case_id}: expected.json has no tool_calls to check.")
        return 2

    reports = check(expected.tool_calls, results, paths)
    for report in reports:
        print(f"{report.worker}: {report.tool_calls_used} tool call(s), {report.repeats} repeat(s)")
        print("\n".join(f"  {c.line()}" for c in report.calls) or "  (no calls)")
        print("\n".join(f"  FAIL {failure}" for failure in report.failures) or "  ok")
    failed = sum(len(r.failures) for r in reports)
    print(f"\n{expected.case_id}: {failed} failure(s).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
