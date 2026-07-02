const SESSION_STORAGE_KEY = "cosmic-agent-session-id";

export function getOrCreateSessionId(): string {
  const querySessionId = new URLSearchParams(window.location.search)
    .get("session_id")
    ?.trim();
  if (querySessionId) {
    storeSessionId(querySessionId);
    return querySessionId;
  }

  try {
    const existing = window.localStorage.getItem(SESSION_STORAGE_KEY);
    if (existing) {
      return existing;
    }
  } catch {
    // Continue with an in-memory session id when localStorage is blocked.
  }

  const next = createSessionId();
  storeSessionId(next);
  return next;
}

export function storeSessionId(sessionId: string): void {
  try {
    window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  } catch {
    // Storage can be unavailable in restricted browser contexts; the in-memory id
    // still works.
  }
}

export function createSessionId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }

  const randomValues =
    typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function"
      ? crypto.getRandomValues(new Uint8Array(16))
      : Uint8Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));

  randomValues[6] = (randomValues[6] & 0x0f) | 0x40;
  randomValues[8] = (randomValues[8] & 0x3f) | 0x80;

  const hex = [...randomValues].map((value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}
