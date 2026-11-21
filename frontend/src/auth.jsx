import { createContext, useCallback, useContext, useMemo, useState } from "react";
import { ApiError, NO_PERMISSION, request } from "./api.js";
import { log } from "./log.js";

// The session (token, user, expiry) is kept in localStorage, so a reload keeps you logged in.
export const SESSION_KEY = "kyb-review-session";

const AuthContext = createContext(null);

function loadSession() {
  try {
    const session = JSON.parse(localStorage.getItem(SESSION_KEY));
    return session && session.expiresAt > Date.now() ? session : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [session, setSession] = useState(loadSession);

  const logout = useCallback(() => {
    try {
      localStorage.removeItem(SESSION_KEY);
    } catch {
      // storage unavailable: the session only lived in memory
    }
    setSession(null);
  }, []);

  // Logged (log.js) with the user and role, or the status of a refusal: never the password or the token.
  const login = useCallback(async (username, password) => {
    let data;
    try {
      data = await request("/auth/login", { method: "POST", body: { username, password } });
    } catch (error) {
      log.warn("login.failed", { status: error.status, request_id: error.requestId });
      throw error;
    }
    const next = { token: data.access_token, user: data.user, expiresAt: Date.now() + data.expires_in * 1000 };
    try {
      localStorage.setItem(SESSION_KEY, JSON.stringify(next));
    } catch {
      // storage unavailable: stay logged in until this tab closes
    }
    setSession(next);
    log.info("login", { user_id: data.user.user_id, role: data.user.role });
  }, []);

  // Calls the API with the session's token. A 401 means the session is over, so it logs out.
  // A 403 only means this user may not do that: the session goes on, and the page shows
  // NO_PERMISSION instead of the backend's wording.
  const call = useCallback(
    async (path, options = {}) => {
      try {
        return await request(path, { ...options, token: session?.token });
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) logout();
        if (error instanceof ApiError && error.status === 403) throw new ApiError(403, NO_PERMISSION, error.requestId);
        throw error;
      }
    },
    [session, logout],
  );

  const value = useMemo(() => ({ user: session?.user ?? null, login, logout, call }), [session, login, logout, call]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}
