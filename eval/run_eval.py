"""Run the affirm-agent review graph on every golden case and score it.

    .venv/bin/python run_eval.py                         # every golden case, real LLMs
    .venv/bin/python run_eval.py --only ec1 --limit 5    # a quick slice
    .venv/bin/python run_eval.py --lead-llm off          # deterministic Lead (workers still use the LLM)
    .venv/bin/python run_eval.py --judge                 # also LLM-judge the forbidden actions rules can't decide

The agent is imported from this repo (the folder above eval/) and run in-process, exactly as
services/agent/app/main.py wires it (Lead LLM, Records/Evidence workers, LangFuse
callbacks if configured) except that the case service is replaced by the agent's own
FakeCaseClient over data/golden. That client loads every world.json into shared
tables, as the seeded case service does, so registry searches can find look-alikes
from other cases. expected.json is never given to the agent.

The graph pauses at step 10 (analyst decision); the evaluation reads the ReviewOutput
there and doesn't resume it.

Output: results/<timestamp>/results.jsonl (one line per case) and report.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_REPO = Path(os.environ.get("AFFIRM_AGENT_DIR", HERE.parent)).resolve()
GOLDEN = AGENT_REPO / "data" / "golden"
sys.path[:0] = [str(AGENT_REPO / "services" / "agent"), str(AGENT_REPO / "contracts")]


def load_env(path: Path) -> None:
    """Read KEY=VALUE lines from the agent's .env without overriding the shell."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value and key.strip() not in os.environ:
            os.environ[key.strip()] = value


def golden_cases(only: list[str] | None) -> list[Path]:
    folders = sorted(p.parent for p in GOLDEN.rglob("expected.json"))
    if only:
        folders = [f for f in folders if any(o in (f.parent.name, f.name) for o in only)]
    return folders


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="edge-case folders (ec1, ec2, ec4, combo) or case IDs")
    ap.add_argument("--limit", type=int, help="run at most N cases")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--lead-llm", choices=["on", "off"], help="override LEAD_LLM from .env")
    ap.add_argument("--model", help="override OPENAI_MODEL (workers and Lead)")
    ap.add_argument("--today", help="review date YYYY-MM-DD (default: today)")
    ap.add_argument("--judge", action="store_true", help="LLM-judge the forbidden actions a rule can't decide")
    ap.add_argument("--dump", action="store_true", help="also save each case's raw worker results and review output")
    ap.add_argument("--out", type=Path, help="output folder (default results/<timestamp>)")
    args = ap.parse_args()

    load_env(AGENT_REPO / ".env")
    if args.lead_llm:
        os.environ["LEAD_LLM"] = args.lead_llm
    if args.model:
        os.environ["OPENAI_MODEL"] = args.model
    os.environ.setdefault("LOG_LEVEL", "WARNING")

    # Import after the env is set: the agent reads it at import/build time.
    from app.clients.case_client import FakeCaseClient
    from app.graph.llm import default_llm
    from app.graph.worker_adapter import build_workers
    from app.graph.workflow import Deps, build_graph
    from app.observability.tracing import langfuse_callbacks
    from kyb_contracts.golden import ExpectedAnswer
    from scoring import score_case

    folders = golden_cases(args.only)[: args.limit]
    if not folders:
        print("no cases matched", file=sys.stderr)
        return 1
    review_day = date.fromisoformat(args.today) if args.today else date.today()
    now = datetime.combine(review_day, dtime(12, 0), tzinfo=timezone.utc)
    clock = lambda: now  # noqa: E731

    client = FakeCaseClient.from_dir(GOLDEN)   # every case, as the seeded case service holds them
    callbacks = langfuse_callbacks()
    records, evidence = build_workers(client, callbacks=callbacks, clock=clock)
    llm = default_llm(callbacks)
    graph = build_graph(Deps(records_worker=records, evidence_worker=evidence, llm=llm, clock=clock,
                             result_sink=None, callbacks=callbacks))

    out = args.out or HERE / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    meta = {"started": datetime.now().isoformat(timespec="seconds"), "review_day": review_day.isoformat(),
            "cases": len(folders), "lead_llm": type(llm).__name__, "lead_llm_env": os.getenv("LEAD_LLM"),
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "lead_model": os.getenv("LEAD_MODEL") or None,
            "workers": [type(records).__name__, type(evidence).__name__], "langfuse": bool(callbacks),
            "judge": args.judge}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta), flush=True)

    judge = None
    if args.judge:
        from judge import Judge
        judge = Judge(os.getenv("JUDGE_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")))

    def run_one(folder: Path) -> dict:
        # Validated against the contract, then scored as plain JSON (RecordRefs as {source, where}).
        expected = ExpectedAnswer.model_validate_json(
            (folder / "expected.json").read_text(encoding="utf-8")).model_dump(mode="json")
        raw_case = json.loads((folder / "case.json").read_text(encoding="utf-8"))
        review_id = f"eval-{folder.name}"
        cfg = {"configurable": {"thread_id": review_id}, "callbacks": callbacks,
               "metadata": {"case_id": folder.name, "eval": True}}
        t0 = time.monotonic()
        row = {"case_id": folder.name, "group": folder.parent.name, "edge_cases": expected.get("edge_cases"),
               "expected_decision": expected["expected_decision"], "notes": expected.get("notes")}
        try:
            graph.invoke({"review_id": review_id, "raw_case": raw_case}, cfg)
            state = graph.get_state(cfg).values
            results = [r["result"] for r in state.get("worker_results") or []]
            row.update(score_case(expected, state.get("review_output"), results, state.get("internal_rounds", 0)))
            row["stop_reason"] = state.get("stop_reason")
            row["route"] = (state.get("routing") or {}).get("route")
            row["worker_errors"] = [r.get("error") for r in results if r.get("status") != "ok" and r.get("error")]
            row["worker_statuses"] = [f"{r.get('worker')}:{r.get('status')}" for r in results]
            row["tool_calls"] = sum(r.get("tool_calls_used") or 0 for r in results)
            memo = ((state.get("review_output") or {}).get("checked_memo") or {}).get("memo")
            row["notes_for_analyst"] = (memo or {}).get("notes_for_analyst")
            if args.dump:
                row["raw"] = {"worker_results": results, "review_output": state.get("review_output")}
            if judge:
                judge.fill(row, expected, memo, state.get("review_output"))
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc()
        row["seconds"] = round(time.monotonic() - t0, 1)
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool, open(out / "results.jsonl", "w") as fh:
        futures = {pool.submit(run_one, f): f for f in folders}
        for n, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            rows.append(row)
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
            mark = "ERR" if row.get("error") else ("ok " if row.get("decision_correct") else "BAD")
            print(f"[{n:3}/{len(folders)}] {mark} {row['case_id']} expected={row['expected_decision']} "
                  f"got={row.get('decision')} cite={row.get('must_cite_recall', 0):.2f} {row['seconds']}s",
                  flush=True)

    from report import write_report
    write_report(out)
    print(f"\nreport: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
