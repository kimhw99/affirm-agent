import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth.jsx";
import { METRICS_ROLES, label } from "./format.js";
import Login from "./pages/Login.jsx";
import Metrics from "./pages/Metrics.jsx";
import Queue from "./pages/Queue.jsx";
import Review from "./pages/Review.jsx";

function RequireLogin({ children }) {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return children;
}

// A page for some roles only. Anyone else gets a notice instead, so the page never loads
// and sends no request.
function RequireRole({ roles, children }) {
  const { user } = useAuth();
  if (roles.includes(user.role)) return children;
  return (
    <main className="page">
      <p className="error" role="alert">
        You don't have access to this page.
      </p>
      <Link to="/">← Case queue</Link>
    </main>
  );
}

function Shell({ children }) {
  const { user, logout } = useAuth();
  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">
          KYB Review
        </Link>
        {METRICS_ROLES.includes(user.role) && <Link to="/metrics">Metrics</Link>}
        <span className="who">
          {user.display_name} · {label(user.role)}
        </span>
        <button type="button" className="link" onClick={logout}>
          Log out
        </button>
      </header>
      {children}
    </>
  );
}

function Page({ children }) {
  return (
    <RequireLogin>
      <Shell>{children}</Shell>
    </RequireLogin>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<Page><Queue /></Page>} />
      <Route path="/cases/:caseId" element={<Page><Review /></Page>} />
      <Route path="/metrics" element={<Page><RequireRole roles={METRICS_ROLES}><Metrics /></RequireRole></Page>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

// Opts in to React Router v7's behaviour now, so the upgrade won't change anything.
export const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter future={ROUTER_FUTURE}>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}
