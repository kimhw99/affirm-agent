import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import Checks from "../components/Checks.jsx";
import DecisionForm from "../components/DecisionForm.jsx";
import DocumentForm from "../components/DocumentForm.jsx";
import { EvidencePanel } from "../components/Evidence.jsx";
import Memo from "../components/Memo.jsx";
import { allowedDecisions, canDecide, documentLabel, formatDate, label, readOnlyReason } from "../format.js";

const POLL_MS = 2000;

function CaseHeader({ kase }) {
  const app = kase.application;
  return (
    <section className="card case-head">
      <div className="case-title">
        <h1>{app.legal_name}</h1>
        <span className={`badge status-${kase.status}`}>{label(kase.status)}</span>
      </div>
      <p className="muted">
        {kase.case_id} · {app.state} · {app.entity_type}
        {app.dba && ` · DBA ${app.dba}`}
        {app.tin_type && ` · ${app.tin_type} ending ${app.tin_last4}`}
      </p>
      <p>
        {kase.flag_reasons.map((flag) => (
          <code key={flag} className="flag">
            {flag}
          </code>
        ))}
      </p>
    </section>
  );
}

function DecisionSaved({ record, status }) {
  return (
    <section className="card decision-saved" role="status">
      <h2>Decision saved</h2>
      <p>
        <strong>{label(record.decision)}</strong> by {record.analyst_id}, {formatDate(record.decided_at)}
        {record.overridden && " · against the agent's recommendation"}
      </p>
      {record.note && <p>{record.note}</p>}
      <p className="muted">The case is now {label(status)}.</p>
    </section>
  );
}

function History({ status, decisions }) {
  return (
    <section className="card">
      <h2>Decisions</h2>
      <p className="muted">{readOnlyReason(status)}</p>
      {decisions === null && <p className="muted">Loading…</p>}
      {decisions?.length === 0 && <p className="muted">No decisions recorded.</p>}
      {decisions?.length > 0 && (
        <ul>
          {decisions.map((d) => (
            <li key={d.review_id}>
              <strong>{label(d.decision)}</strong> by {d.analyst_id}, {formatDate(d.decided_at)}
              {d.overridden && " (override)"}
              {d.note && ` — ${d.note}`}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function Review() {
  const { caseId } = useParams();
  const { user, call } = useAuth();
  const [kase, setCase] = useState(null);
  const [review, setReview] = useState(null);
  const [history, setHistory] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [notice, setNotice] = useState("");

  // POST /reviews starts a review, or returns the one already running or waiting for this case.
  const startReview = useCallback(async () => {
    setError("");
    try {
      setReview(await call("/reviews", { method: "POST", body: { case_id: caseId } }));
    } catch (err) {
      setError(`Couldn't start the review: ${err.message}`);
    }
  }, [call, caseId]);

  // A case this user may decide gets its review when opened. Any other case is read-only:
  // the page says why and shows its decisions, and never calls /reviews.
  useEffect(() => {
    let active = true;
    setCase(null);
    setReview(null);
    setHistory(null);
    setError("");
    setSelected(null);
    setNotice("");
    call(`/cases/${caseId}`)
      .then(async (loaded) => {
        if (!active) return;
        setCase(loaded);
        if (canDecide(user.role, loaded.status)) {
          await startReview();
        } else {
          const decisions = await call(`/cases/${caseId}/decisions`);
          if (active) setHistory(decisions);
        }
      })
      .catch((err) => active && setError(err.message));
    return () => {
      active = false;
    };
  }, [call, caseId, startReview, user.role]);

  // While the agent works, ask again every couple of seconds.
  useEffect(() => {
    if (review?.status !== "running") return undefined;
    const timer = setTimeout(() => {
      call(`/reviews/${review.review_id}`)
        .then(setReview)
        .catch((err) => setError(err.message));
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [review, call]);

  async function decided(result) {
    setReview(result);
    try {
      setCase(await call(`/cases/${caseId}`));
    } catch {
      // the decision is saved either way; the status shown may lag
    }
  }

  // A document the merchant sent brings a docs_requested case back to pending_review. The
  // case is read again, and as for any case this user may decide, its new review starts.
  async function added(result) {
    setNotice(`Added ${result.document.id}: ${documentLabel(result.document.type)}. ` +
              `The case is back in ${label(result.case_status)}, so a new review starts.`);
    try {
      const loaded = await call(`/cases/${caseId}`);
      setCase(loaded);
      setHistory(null);
      if (canDecide(user.role, loaded.status)) await startReview();
    } catch (err) {
      setError(err.message);
    }
  }

  if (!kase) {
    return (
      <main className="page">
        {error ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : (
          <p className="muted">Loading the case…</p>
        )}
        <Link to="/">← Case queue</Link>
      </main>
    );
  }

  const decides = canDecide(user.role, kase.status);
  const output = review?.review_output;
  const index = { ...(output?.report_evidence_index ?? {}), ...(output?.checked_memo?.evidence_index ?? {}) };
  return (
    <main className="page">
      <p>
        <Link to="/">← Case queue</Link>
      </p>
      <CaseHeader kase={kase} />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <section className="card decision-saved" role="status">
          <p>{notice}</p>
        </section>
      )}
      {/* Waiting for the merchant: the documents they sent bring the case back to review. */}
      {kase.status === "docs_requested" && <DocumentForm caseId={caseId} onAdded={added} />}
      {!review && !decides && <History status={kase.status} decisions={history} />}
      {!review && decides && !error && <p className="muted">Starting the review…</p>}
      {review?.status === "running" && (
        <p className="muted" role="status">
          The agent is reviewing this case. This page updates by itself.
        </p>
      )}
      {review?.status === "failed" && (
        <section className="card">
          <p className="error">The review failed: {review.error}</p>
          <button type="button" onClick={startReview}>
            Start it again
          </button>
        </section>
      )}
      {output && (
        <div className="review-layout">
          <div className="review-main">
            <Memo output={output} selected={selected} onSelect={setSelected} />
            <Checks checks={output.lead?.checks} selected={selected} onSelect={setSelected} />
            {review.status === "awaiting_decision" && (
              <DecisionForm
                reviewId={review.review_id}
                recommendation={output.checked_memo?.memo?.recommendation}
                decisions={allowedDecisions(user.role, kase.status)}
                onDecided={decided}
              />
            )}
            {review.status === "done" && review.decision_record && (
              <DecisionSaved record={review.decision_record} status={kase.status} />
            )}
          </div>
          <EvidencePanel id={selected} index={index} onClose={() => setSelected(null)} />
        </div>
      )}
    </main>
  );
}
