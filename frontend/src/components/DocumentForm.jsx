import { useState } from "react";
import { useAuth } from "../auth.jsx";
import { DATE_FIELDS, DOCUMENT_TYPES, fieldLabel } from "../format.js";

// The browser's hints for a field: 4 digits for the last 4 of a tax ID, a format for a date.
function hints(field) {
  if (field.endsWith("_last4")) {
    return { inputMode: "numeric", pattern: "\\d{4}", maxLength: 4, placeholder: "1234", title: "The last 4 digits only" };
  }
  return DATE_FIELDS.includes(field) ? { placeholder: "YYYY-MM-DD" } : {};
}

// A document the merchant sent for a docs_requested case. There is no OCR in this project,
// so an analyst types in what the document shows, as the existing system's extraction would
// (POST /cases/{case_id}/documents). Saving it sends the case back to pending_review.
export default function DocumentForm({ caseId, onAdded }) {
  const { call } = useAuth();
  const [type, setType] = useState("");
  const [values, setValues] = useState({});
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fields = DOCUMENT_TYPES.find((option) => option.value === type)?.fields ?? [];

  function choose(next) {
    setType(next);
    setValues({});
  }

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    // Only the fields filled in: a document may not show them all.
    const extracted = Object.fromEntries(
      fields.map((field) => [field, (values[field] ?? "").trim()]).filter(([, value]) => value),
    );
    try {
      const body = { type, extracted, note: note.trim() };
      onAdded(await call(`/cases/${caseId}/documents`, { method: "POST", body }));
    } catch (err) {
      setError(`Couldn't add the document: ${err.message}`);
      setBusy(false);
    }
  }

  return (
    <form className="card decision-form" onSubmit={submit}>
      <h2>Add the documents the merchant sent</h2>
      <p className="muted">
        Type in what the document shows, and leave out what it doesn't. For a tax ID, enter only its last 4 digits,
        never a full SSN or EIN.
      </p>
      <label>
        Document type
        <select value={type} onChange={(e) => choose(e.target.value)} required>
          <option value="">Choose one…</option>
          {DOCUMENT_TYPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      {fields.map((field) => (
        <label key={field}>
          {fieldLabel(field)}
          <input
            value={values[field] ?? ""}
            onChange={(e) => setValues({ ...values, [field]: e.target.value })}
            autoComplete="off"
            {...hints(field)}
          />
        </label>
      ))}
      <label>
        Note
        <textarea value={note} maxLength={2000} rows={3} onChange={(e) => setNote(e.target.value)} />
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" disabled={!type || busy}>
        {busy ? "Adding…" : "Add the document"}
      </button>
    </form>
  );
}
