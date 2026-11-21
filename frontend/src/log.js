// What the app did, in the browser's console (DevTools, Console), one line per event:
//
//   2026-11-20T21:04:05.123Z INFO review.started case_id=case_1042 review_id=rv_1a2b3c4d
//
// that is, the time in UTC, the level, the event's name and its details. A failed API
// call's line has the request_id the gateway gave it, to find the services' lines for it
// (docs/local-testing.md, "Reading the logs").
//
// Never a password, a token or a note: the callers pass IDs, statuses and reasons only.
// As a last guard, a detail named like a secret (password, token, authorization...) is
// written as [redacted], and so is anything shaped like a JWT, a Bearer token or an API key.

const SECRET_NAME = /pass(word)?|token|secret|authori[sz]ation|api[_-]?key/i;
const SECRET_SHAPES = [
  /\beyJ[\w-]*\.[\w-]+\.[\w-]*/g, // a JWT
  /\bbearer\s+[^\s,;'"]+/gi, // an Authorization header's value
  /\b(?:sk|pk)-[\w-]{8,}/g, // an OpenAI or LangFuse key
];

function text(value) {
  const written = typeof value === "string" ? value : JSON.stringify(value);
  return /[\s"=]/.test(written) || written === "" ? JSON.stringify(written) : written;
}

// "name=value" for each detail that has a value.
function details(fields = {}) {
  return Object.entries(fields)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([name, value]) => `${name}=${SECRET_NAME.test(name) ? "[redacted]" : text(value)}`)
    .join(" ");
}

export function line(level, event, fields) {
  const written = [new Date().toISOString(), level, event, details(fields)].filter(Boolean).join(" ");
  return SECRET_SHAPES.reduce((result, shape) => result.replace(shape, "[redacted]"), written);
}

export const log = {
  info: (event, fields) => console.info(line("INFO", event, fields)),
  warn: (event, fields) => console.warn(line("WARNING", event, fields)),
  error: (event, fields) => console.error(line("ERROR", event, fields)),
};
