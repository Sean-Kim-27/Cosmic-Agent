import type {
  CGIParseJob,
  ChatHistoryResponse,
  ChatSessionListResponse,
  ChatSessionClearResponse,
  ChatStreamRequest,
  JobRetryResponse,
  StreamEvent,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const FRONTEND_API_SECRET = import.meta.env.VITE_FRONTEND_API_SECRET ?? "";

export async function fetchJobs(): Promise<CGIParseJob[]> {
  const response = await fetch(`${API_BASE}/api/v1/jobs?limit=100`, {
    headers: authHeaders(),
  });
  if (!response.ok) {
    throw new Error(`Failed to fetch jobs: ${response.status}`);
  }
  return response.json();
}

export async function fetchChatHistory(sessionId: string): Promise<ChatHistoryResponse> {
  const response = await fetch(
    `${API_BASE}/api/v1/chat/history/${encodeURIComponent(sessionId)}`,
    {
      headers: authHeaders(),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch chat history: ${response.status}`);
  }
  return response.json();
}

export async function fetchChatSessions(): Promise<ChatSessionListResponse> {
  const response = await fetch(`${API_BASE}/api/v1/chat/sessions?limit=100`, {
    headers: authHeaders(),
  });
  if (!response.ok) {
    throw new Error(`Failed to fetch chat sessions: ${response.status}`);
  }
  return response.json();
}

export async function clearChatHistory(sessionId: string): Promise<ChatSessionClearResponse> {
  const response = await fetch(`${API_BASE}/api/v1/chat/history/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!response.ok) {
    throw new Error(`Failed to clear chat history: ${response.status}`);
  }
  return response.json();
}

export async function retryAllJobs(): Promise<JobRetryResponse> {
  const response = await fetch(`${API_BASE}/api/v1/jobs/retry`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ statuses: ["FAILED", "QUOTA_LOCKED"], process_limit: 5 }),
  });
  if (!response.ok) {
    throw new Error(`Failed to retry jobs: ${response.status}`);
  }
  return response.json();
}

export async function streamChat(
  payload: ChatStreamRequest,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/v1/chat/stream`, {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) {
    throw new Error(`Chat stream failed: ${response.status}`);
  }
  if (!response.body) {
    throw new Error("Chat stream did not include a response body");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const eventBlock of events) {
      const parsed = parseSseEvent(eventBlock);
      if (parsed) {
        onEvent(parsed);
      }
    }
  }
  if (buffer.trim()) {
    const parsed = parseSseEvent(buffer);
    if (parsed) {
      onEvent(parsed);
    }
  }
}

function parseSseEvent(block: string): StreamEvent | null {
  const lines = block.split("\n");
  const eventLine = lines.find((line) => line.startsWith("event:"));
  const dataLines = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.replace(/^data:\s?/, ""));
  if (!eventLine || dataLines.length === 0) {
    return null;
  }
  const event = eventLine.replace(/^event:\s?/, "") as StreamEvent["event"];
  const data = JSON.parse(dataLines.join("\n"));
  return { event, data } as StreamEvent;
}

function authHeaders(): HeadersInit {
  return FRONTEND_API_SECRET ? { "X-Cosmic-API-Key": FRONTEND_API_SECRET } : {};
}

function jsonHeaders(): HeadersInit {
  return {
    "Content-Type": "application/json",
    ...authHeaders(),
  };
}
