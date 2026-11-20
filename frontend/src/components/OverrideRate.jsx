import { useEffect, useState } from "react";
import { useAuth } from "../auth.jsx";
import { label, percent } from "../format.js";

// BEAC-20: how often analysts decide against the agent's recommendation.
export default function OverrideRate() {
  const { call } = useAuth();
  const [stats, setStats] = useState(null);

  useEffect(() => {
    let active = true;
    call("/metrics/override-rate")
      .then((result) => active && setStats(result))
      .catch(() => active && setStats(null)); // the queue still works without it
    return () => {
      active = false;
    };
  }, [call]);

  if (!stats) return null;
  return (
    <section className="card metrics" aria-label="Override rate">
      <div>
        <h2>Override rate</h2>
        <p className="big">{percent(stats.rate)}</p>
        <p className="muted small">
          {stats.overridden} of {stats.decisions} decisions went against the agent's recommendation.
        </p>
      </div>
      {stats.by_recommendation.length > 0 && (
        <table className="compact">
          <thead>
            <tr>
              <th>The agent recommended</th>
              <th>Decisions</th>
              <th>Overridden</th>
            </tr>
          </thead>
          <tbody>
            {stats.by_recommendation.map((group) => (
              <tr key={group.recommendation}>
                <td>{label(group.recommendation)}</td>
                <td>{group.decisions}</td>
                <td>
                  {group.overridden} ({percent(group.rate)})
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
