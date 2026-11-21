import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import { CASE_STATUSES, defaultQueue, formatDate, label } from "../format.js";

export default function Queue() {
  const { user, call } = useAuth();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? defaultQueue(user.role);
  const [cases, setCases] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setCases(null);
    setError("");
    call(status === "all" ? "/cases" : `/cases?status=${status}`)
      .then((rows) => active && setCases(rows))
      .catch((err) => active && setError(err.message));
    return () => {
      active = false;
    };
  }, [status, call]);

  return (
    <main className="page">
      <div className="toolbar">
        <h1>Case queue</h1>
        <label>
          Status
          <select value={status} onChange={(e) => setParams({ status: e.target.value })}>
            {CASE_STATUSES.map((s) => (
              <option key={s} value={s}>
                {label(s)}
              </option>
            ))}
            <option value="all">All cases</option>
          </select>
        </label>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {cases === null && !error && <p className="muted">Loading…</p>}
      {cases?.length === 0 && <p className="muted">No cases here.</p>}
      {cases?.length > 0 && (
        <table className="cases">
          <thead>
            <tr>
              <th>Case</th>
              <th>Business</th>
              <th>State</th>
              <th>Flags</th>
              <th>Status</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.case_id}>
                <td>
                  <Link to={`/cases/${c.case_id}`}>{c.case_id}</Link>
                </td>
                <td>
                  {c.legal_name}
                  {c.dba && <div className="muted small">DBA {c.dba}</div>}
                </td>
                <td>{c.state}</td>
                <td>
                  {c.flag_reasons.map((flag) => (
                    <code key={flag} className="flag">
                      {flag}
                    </code>
                  ))}
                </td>
                <td>
                  <span className={`badge status-${c.status}`}>{label(c.status)}</span>
                </td>
                <td className="small">{formatDate(c.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
