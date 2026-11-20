import { label } from "../format.js";
import { EvidenceIds } from "./Evidence.jsx";

function show(value) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  return typeof value === "string" ? value : JSON.stringify(value);
}

// The lead's checks (LeadReport.checks): what the records and the merchant's evidence say, and how they compare.
export default function Checks({ checks, selected, onSelect }) {
  if (!checks?.length) return null;
  return (
    <section className="card">
      <h2>Checks</h2>
      <table className="checks">
        <thead>
          <tr>
            <th>Check</th>
            <th>Status</th>
            <th>Official records</th>
            <th>Merchant's evidence</th>
            <th>Evidence</th>
          </tr>
        </thead>
        <tbody>
          {checks.map((check) => (
            <tr key={check.check_id}>
              <td>
                {check.description}
                <div className="muted small">
                  {check.check_id}
                  {!check.required && " · optional"}
                </div>
                {check.explanation && <div className="small">{check.explanation}</div>}
              </td>
              <td>
                <span className={`badge check-${String(check.status).toLowerCase()}`}>{label(check.status)}</span>
                <div className="muted small">{check.confidence} confidence</div>
              </td>
              <td>{show(check.records_value)}</td>
              <td>{show(check.evidence_value)}</td>
              <td>
                <EvidenceIds
                  ids={[...(check.evidence ?? []), ...(check.explanation_evidence ?? [])]}
                  selected={selected}
                  onSelect={onSelect}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
