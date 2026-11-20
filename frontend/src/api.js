// Calls the backend through /api, which nginx (or Vite's dev server) forwards to the gateway.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function request(path, { method = "GET", body, token } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    const text = await response.text();
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null; // not JSON, such as a proxy's error page
  }
  if (!response.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }
  return data;
}
