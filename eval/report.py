"""Turn results/<run>/results.jsonl into report.md.

    .venv/bin/python report.py results/<run>
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DECISIONS = ("recommend_clear", "request_documents", "escalate", "insufficient_evidence")


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "–"


def load(run: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run / "meta.json").read_text()) if (run / "meta.json").is_file() else {}
    rows = [json.loads(l) for l in (run / "results.jsonl").read_text().splitlines() if l.strip()]
    rows.sort(key=lambda r: r["case_id"])
    return meta, rows


def write_report(run: Path) -> Path:
    meta, rows = load(run)
    ok = [r for r in rows if not r.get("error")]
    L: list[str] = []
    w = L.append

    w(f"# affirm-agent golden-set evaluation\n")
    w(f"Run `{run.name}` · review date {meta.get('review_day')} · model `{meta.get('model')}` · "
      f"Lead `{meta.get('lead_llm')}` · workers {', '.join(meta.get('workers', []))} · "
      f"judge {'on' if meta.get('judge') else 'off'}\n")

    # ---- headline ----
    n = len(rows)
    dec = sum(r.get("decision_correct", False) for r in ok)
    cite_all = sum(r.get("must_cite_all", False) for r in ok)
    recall = sum(r.get("must_cite_recall", 0) for r in ok) / len(ok) if ok else 0
    forb = [f for r in ok for f in r.get("forbidden", []) if f.get("violated") != "n/a"]
    viol = [f for f in forb if f.get("violated") is True]
    undecided = [f for f in forb if f.get("violated") is None]
    strict = sum(r.get("decision_correct") and r.get("must_cite_all")
                 and not any(f.get("violated") is True for f in r.get("forbidden", [])) for r in ok)
    w("## Headline\n")
    w("| Metric | Result |\n|---|---|")
    w(f"| Cases run | {n} ({n - len(ok)} crashed) |")
    w(f"| Decision accuracy | {_pct(dec, len(ok))} |")
    w(f"| All must_cite items cited | {_pct(cite_all, len(ok))} |")
    w(f"| must_cite recall (mean per case) | {recall:.0%} |")
    w(f"| Forbidden-action violations | {len(viol)} of {len(forb)} checked "
      f"({len(undecided)} undecided{' — run with --judge' if undecided else ''}) |")
    auto = [r for r in ok if r.get("auto_resolved") and not r.get("memo_flags")]
    w(f"| Closed at intake with no worker run (auto_resolved only) | {len(auto)} |")
    w(f"| Step-9 checks passed | {_pct(sum(r.get('checks_passed', False) for r in ok), len(ok))} |")
    w(f"| Within max_follow_up_rounds | {_pct(sum(r.get('rounds_ok', False) for r in ok), len(ok))} |")
    w(f"| **Strict pass** (decision + all citations + no violation) | **{_pct(strict, len(ok))}** |")
    secs = [r.get("seconds", 0) for r in rows]
    if secs:
        w(f"| Seconds per case (mean / max) | {sum(secs) / len(secs):.0f} / {max(secs):.0f} |")
    w("")

    # ---- by group ----
    w("## By edge-case group\n")
    w("| Group | Cases | Decision | All citations | Cite recall | Strict |\n|---|---|---|---|---|---|")
    groups = defaultdict(list)
    for r in ok:
        groups[r["group"]].append(r)
    for g in sorted(groups):
        rs = groups[g]
        st = sum(r["decision_correct"] and r["must_cite_all"]
                 and not any(f.get("violated") is True for f in r["forbidden"]) for r in rs)
        w(f"| {g} | {len(rs)} | {_pct(sum(r['decision_correct'] for r in rs), len(rs))} | "
          f"{_pct(sum(r['must_cite_all'] for r in rs), len(rs))} | "
          f"{sum(r['must_cite_recall'] for r in rs) / len(rs):.0%} | {_pct(st, len(rs))} |")
    w("")

    # ---- confusion ----
    w("## Decision confusion matrix (rows = expected, columns = agent)\n")
    cols = list(DECISIONS) + ["(none)"]
    w("| expected \\ got | " + " | ".join(cols) + " |")
    w("|---" * (len(cols) + 1) + "|")
    cm = Counter((r["expected_decision"], r.get("decision") or "(none)") for r in ok)
    for e in DECISIONS:
        if any(k[0] == e for k in cm):
            w(f"| {e} | " + " | ".join(str(cm.get((e, c), "")) for c in cols) + " |")
    w("")

    # ---- citations ----
    w("## must_cite misses\n")
    w("`retrieved` = a worker fetched a matching record but the memo didn't cite it; "
      "`never retrieved` = no worker looked it up.\n")
    miss = Counter()
    for r in ok:
        for c in r["must_cite"]:
            if not c["cited"]:
                kind = c["item"].split("[")[0]
                miss[(kind, "retrieved, not cited" if c["retrieved"] else "never retrieved")] += 1
    w("| Item kind | Why missed | Count |\n|---|---|---|")
    for (k, why), c in miss.most_common():
        w(f"| `{k}` | {why} | {c} |")
    w("")

    # ---- forbidden ----
    w("## Forbidden-action violations\n")
    if viol:
        vc = Counter((f["action"], f.get("by")) for f in viol)
        w("| Action | Decided by | Cases |\n|---|---|---|")
        for (a, by), c in vc.most_common():
            w(f"| {a} | {by} | {c} |")
    else:
        w("None found.")
    w("")

    # ---- check failures ----
    cf = Counter(x for r in ok for x in r.get("check_failures") or [])
    if cf:
        w("## Step-9 check failures (final memo)\n")
        w("| Rule | Cases |\n|---|---|")
        for rule, c in cf.most_common():
            w(f"| {rule} | {c} |")
        w("")

    werr = Counter(e[:120] for r in ok for e in r.get("worker_errors") or [])
    if werr:
        w("## Worker errors\n")
        for e, c in werr.most_common(10):
            w(f"- {c}× `{e}`")
        w("")

    # ---- per case failures ----
    w("## Cases that failed\n")
    w("| Case | Expected | Got | Missing citations | Violations | Stop reason |\n|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            w(f"| {r['case_id']} | {r['expected_decision']} | **crash** | {r['error'][:80]} | | |")
            continue
        missing = [c["item"] for c in r["must_cite"] if not c["cited"]]
        v = [f["action"] for f in r["forbidden"] if f.get("violated") is True]
        if r["decision_correct"] and not missing and not v:
            continue
        got = r["decision"] if r["decision_correct"] else f"**{r['decision']}**"
        w(f"| {r['case_id']} | {r['expected_decision']} | {got} | {'; '.join(missing)} | "
          f"{'; '.join(v)} | {r.get('stop_reason') or ''} |")
    w("")

    path = run / "report.md"
    path.write_text("\n".join(L))
    return path


if __name__ == "__main__":
    print(write_report(Path(sys.argv[1])))
