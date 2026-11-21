import { Link } from "react-router-dom";
import OverrideRate from "../components/OverrideRate.jsx";

// The metrics page. Only senior analysts reach it: App.jsx wraps it in RequireRole, as the
// gateway answers 403 to anyone else (docs/auth.md, Roles).
export default function Metrics() {
  return (
    <main className="page">
      <p>
        <Link to="/">← Case queue</Link>
      </p>
      <h1>Metrics</h1>
      <OverrideRate />
    </main>
  );
}
