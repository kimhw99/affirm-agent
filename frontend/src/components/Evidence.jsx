import { label } from "../format.js";

// Evidence IDs (R1, D1, B1, W1, K1) as buttons that open the evidence panel.
export function EvidenceIds({ ids, selected, onSelect }) {
  const unique = [...new Set(ids ?? [])];
  if (unique.length === 0) return null;
  return (
    <span className="evidence-ids">
      {unique.map((id) => (
        <button
          key={id}
          type="button"
          className={`evidence${id === selected ? " selected" : ""}`}
          aria-pressed={id === selected}
          onClick={() => onSelect(id)}
        >
          {id}
        </button>
      ))}
    </span>
  );
}

// What an evidence ID points to: the source, its type and date, and the excerpt Checks took from it.
export function EvidencePanel({ id, index, onClose }) {
  if (!id) {
    return <aside className="card evidence-panel muted">Click an evidence ID to see where it comes from.</aside>;
  }
  const entry = index[id];
  return (
    <aside className="card evidence-panel" aria-label={`Evidence ${id}`}>
      <div className="panel-head">
        <h2>{id}</h2>
        <button type="button" className="link" onClick={onClose}>
          Close
        </button>
      </div>
      {entry ? (
        <dl>
          <dt>Source</dt>
          <dd>{entry.source}</dd>
          <dt>Type</dt>
          <dd>{label(entry.type)}</dd>
          {entry.as_of && (
            <>
              <dt>As of</dt>
              <dd>{entry.as_of}</dd>
            </>
          )}
          <dt>Excerpt</dt>
          <dd className="excerpt">{entry.excerpt}</dd>
        </dl>
      ) : (
        <p className="muted">No details for {id}.</p>
      )}
    </aside>
  );
}
