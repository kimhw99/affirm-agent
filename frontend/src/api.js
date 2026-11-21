// Calls the backend through /api, which nginx (or Vite's dev server) forwards to the gateway.
// A failed call is logged (log.js) with its method, path, status and request ID, never
// its token or body: a warning for a 4xx answer, an error for a 5xx or no answer at all.

import { log } from "./log.js";

// What the app says when the backend refuses an action for this user (403).
export const NO_PERMISSION = "You don't have permission to do this.";

export class ApiError extends Error {
  // requestId: the X-Request-ID the gateway answered with, to find the call in its logs.
  constructor(status, message, requestId = null) {
    super(message);
    this.status = status;
    this.requestId = requestId;
  }
}

// The backend's reason for an error: FastAPI's detail is a message, or a list of
// validation errors, each with where it is and its own message.
function reason(detail, status) {
  if (typeof detail === "string") return detail;
  const messages = (Array.isArray(detail) ? detail : [])
    .filter((error) => typeof error?.msg === "string")
    .map((error) => {
      const where = (Array.isArray(error.loc) ? error.loc : []).filter((part) => part !== "body").join(".");
      const message = error.msg.replace(/^Value error, /, "");
      return where ? `${where}: ${message}` : message;
    });
  return messages.join("; ") || `HTTP ${status}`;
}

export async function request(path, { method = "GET", body, token } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  const where = { method, path: path.split("?")[0] };
  let response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    log.error("api.unreachable", { ...where, reason: error.message });
    throw error;
  }
  let data = null;
  try {
    const text = await response.text();
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null; // not JSON, such as a proxy's error page
  }
  if (!response.ok) {
    const error = new ApiError(response.status, reason(data?.detail, response.status), response.headers.get("X-Request-ID"));
    (response.status >= 500 ? log.error : log.warn)("api.failed", {
      ...where,
      status: error.status,
      request_id: error.requestId,
      reason: error.message,
    });
    throw error;
  }
  return data;
}
