import { useState } from "react";
import { useAuth } from "../auth.jsx";
import { DECISIONS, isOverride, label } from "../format.js";

// Step 10: the analyst's decision, which resumes the review (POST /reviews/{id}/decision).
export default function DecisionForm({ reviewId, recommendation, onDecided }) {
  const { call } = useAuth();
  const [decision, setDecision] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onDecided(await call(`/reviews/${reviewId}/decision`, { method: "POST", body: { decision, note } }));
    } catch (err) {
      setError(`Couldn't save the decision: ${err.message}`);
      setBusy(false);
    }
  }

  return (
    <form className="card decision-form" onSubmit={submit}>
      <h2>Your decision</h2>
      {recommendation && (
        <p className="muted">
          The agent recommends: <strong>{label(recommendation)}</strong>
        </p>
      )}
      <fieldset>
        <legend>Decision</legend>
        {DECISIONS.map((option) => (
          <label key={option.value} className="choice">
            <input
              type="radio"
              name="decision"
              value={option.value}
              checked={decision === option.value}
              onChange={() => setDecision(option.value)}
            />
            {option.label}
          </label>
        ))}
      </fieldset>
      {decision && recommendation && isOverride(recommendation, decision) && (
        <p className="warn">This goes against the agent's recommendation, so it counts as an override. Say why in the note.</p>
      )}
      <label>
        Note
        <textarea value={note} maxLength={2000} rows={4} onChange={(e) => setNote(e.target.value)} />
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" disabled={!decision || busy}>
        {busy ? "Saving…" : "Submit decision"}
      </button>
    </form>
  );
}
