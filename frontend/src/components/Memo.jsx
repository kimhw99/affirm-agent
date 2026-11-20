import { label } from "../format.js";
import { EvidenceIds } from "./Evidence.jsx";

function Flags({ flags }) {
  return flags.map((flag) => (
    <code key={flag} className="flag">
      {flag}
    </code>
  ));
}

// The lead's memo (memo.py), with what Checks found (check_result).
export default function Memo({ output, selected, onSelect }) {
  const memo = output.checked_memo?.memo;
  const result = output.checked_memo?.check_result;
  if (!memo) {
    return (
      <section className="card">
        <h2>Agent's memo</h2>
        <p className="muted">The agent wrote no memo for this case. {output.lead?.route_reason}</p>
      </section>
    );
  }
  return (
    <section className="card memo">
      <div className="memo-head">
        <h2>Agent's memo</h2>
        <span className={`badge rec-${memo.recommendation}`}>{label(memo.recommendation)}</span>
        {output.lead?.confidence && <span className="muted small">{label(output.lead.confidence)} confidence</span>}
        {result && <span className={`badge ${result.passed ? "ok" : "warn"}`}>{result.passed ? "Checks passed" : "Checks failed"}</span>}
      </div>
      {result && !result.passed && (
        <ul className="failures">
          {result.failures.map((failure, i) => (
            <li key={i}>
              <code>{failure.rule}</code> {failure.detail}
            </li>
          ))}
        </ul>
      )}

      <h3>Flags</h3>
      {memo.flags.map((outcome) => (
        <div key={outcome.flag} className="flag-outcome">
          <div>
            <code className="flag">{outcome.flag}</code>
            <span className={`badge flag-${outcome.status}`}>{label(outcome.status)}</span>
          </div>
          {outcome.root_cause && <p>{outcome.root_cause}</p>}
          <EvidenceIds ids={outcome.evidence} selected={selected} onSelect={onSelect} />
        </div>
      ))}
      {memo.auto_resolved.length > 0 && (
        <p className="muted small">
          Resolved at intake: <Flags flags={memo.auto_resolved} />
        </p>
      )}
      {memo.out_of_scope.length > 0 && (
        <p className="muted small">
          Not reviewed by the agent: <Flags flags={memo.out_of_scope} />
        </p>
      )}

      {memo.ruled_out.length > 0 && (
        <>
          <h3>Ruled out</h3>
          <ul>
            {memo.ruled_out.map((cause, i) => (
              <li key={i}>
                {cause.cause} <EvidenceIds ids={cause.evidence} selected={selected} onSelect={onSelect} />
              </li>
            ))}
          </ul>
        </>
      )}
      {memo.document_request.length > 0 && (
        <>
          <h3>Documents to request</h3>
          <ul>
            {memo.document_request.map((request, i) => (
              <li key={i}>
                <strong>{label(request.type)}</strong>: {request.reason}
              </li>
            ))}
          </ul>
        </>
      )}
      {memo.notes_for_analyst && (
        <>
          <h3>Notes</h3>
          <p>{memo.notes_for_analyst}</p>
        </>
      )}
    </section>
  );
}
