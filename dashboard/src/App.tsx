import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { clearChatHistory, fetchChatHistory, fetchChatSessions, fetchJobs, retryAllJobs, streamChat } from "./api";
import type { CGIParseJob, ChatMessage, ChatSessionSummary } from "./types";

const SESSION_STORAGE_KEY = "cosmic-agent-session-id";

export default function App() {
  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Cosmic Agent</p>
          <h1>Streaming chat + CGI job monitor</h1>
        </div>
        <p className="hero-copy">
          Minimal launch dashboard for testing live SSE responses and background parse queue
          health.
        </p>
      </header>
      <section className="grid">
        <ChatPanel />
        <JobMonitor />
      </section>
    </main>
  );
}

function ChatPanel() {
  const [sessionId, setSessionId] = useState(getOrCreateSessionId);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState(sessionId);
  const [message, setMessage] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [parseCgi, setParseCgi] = useState(true);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState("Idle");
  const [isStreaming, setIsStreaming] = useState(false);
  const [historyStatus, setHistoryStatus] = useState("History not loaded yet");
  const abortRef = useRef<AbortController | null>(null);

  const history = useMemo(
    () =>
      messages
        .filter((item): item is Extract<ChatMessage, { role: "user" | "assistant" }> =>
          item.role === "user" || item.role === "assistant",
        )
        .slice(-12),
    [messages],
  );

  const refreshSessions = useCallback(async () => {
    const payload = await fetchChatSessions();
    setSessions(payload.sessions);
  }, []);

  const switchSession = useCallback((nextSessionId: string) => {
    setSessionId(nextSessionId);
    setSelectedSessionId(nextSessionId);
    storeSessionId(nextSessionId);
  }, []);

  useEffect(() => {
    let active = true;
    setSelectedSessionId(sessionId);
    setHistoryStatus("Restoring previous session...");
    fetchChatHistory(sessionId)
      .then((payload) => {
        if (!active) {
          return;
        }
        setMessages(
          payload.messages.map((item) => ({
            role: item.role,
            content: item.content,
          })),
        );
        setHistoryStatus(
          `Restored ${payload.messages.length} messages · ${payload.cgi_context.interactions.length} CGI memories`,
        );
      })
      .catch((error) => {
        if (!active) {
          return;
        }
        setMessages([]);
        setHistoryStatus(error instanceof Error ? error.message : "History restore failed");
      });
    refreshSessions().catch((error) => {
      if (active) {
        setHistoryStatus(error instanceof Error ? error.message : "Session list failed");
      }
    });
    return () => {
      active = false;
    };
  }, [sessionId, refreshSessions]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = message.trim();
    if (!trimmed || isStreaming) {
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;
    setIsStreaming(true);
    setStatus("Connecting to stream...");
    setMessage("");
    setMessages((current) => [
      ...current,
      { role: "user", content: trimmed },
      { role: "assistant", content: "" },
    ]);

    try {
      await streamChat(
        {
          message: trimmed,
          provider: provider.trim() || undefined,
          model: model.trim() || undefined,
          session_id: sessionId,
          parse_cgi: parseCgi,
          history,
        },
        (event) => {
          if (event.event === "metadata") {
            setStatus(`Streaming from ${event.data.provider} · ${event.data.model}`);
          } else if (event.event === "token") {
            setMessages((current) => appendAssistantDelta(current, event.data.text));
          } else if (event.event === "done") {
            setStatus(`Done · CGI parse ${event.data.parse_cgi ? "queued" : "disabled"}`);
          } else if (event.event === "error") {
            setStatus(`Error: ${event.data.message}`);
          }
        },
        controller.signal,
      );
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown stream error");
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
      refreshSessions().catch(() => undefined);
    }
  }

  function stopStream() {
    abortRef.current?.abort();
    setStatus("Stream cancelled");
    setIsStreaming(false);
  }

  function startNewSession() {
    if (isStreaming) {
      return;
    }
    const nextSessionId = createSessionId();
    setMessage("");
    setMessages([]);
    setHistoryStatus("Started a new empty session");
    setStatus("Idle");
    switchSession(nextSessionId);
  }

  async function handleSelectSession(nextSessionId: string) {
    if (!nextSessionId || nextSessionId === sessionId || isStreaming) {
      setSelectedSessionId(sessionId);
      return;
    }
    switchSession(nextSessionId);
  }

  async function handleClearCurrentSession() {
    if (isStreaming) {
      return;
    }
    try {
      const result = await clearChatHistory(sessionId);
      setMessages([]);
      setHistoryStatus(`Cleared ${result.deleted_messages} persisted messages`);
      setStatus("Session cleared");
      await refreshSessions();
    } catch (error) {
      setHistoryStatus(error instanceof Error ? error.message : "Session clear failed");
    }
  }

  const currentSessionLabel = sessionId.slice(0, 8);

  return (
    <section className="panel chat-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Live SSE</p>
          <h2>Chat</h2>
        </div>
        <span className="status-pill">{status}</span>
      </div>
      <div className="session-row">
        <span title={sessionId}>Session: {currentSessionLabel}</span>
        <span>{historyStatus}</span>
      </div>
      <div className="session-tools">
        <label>
          Saved sessions
          <select
            disabled={isStreaming}
            value={selectedSessionId}
            onChange={(event) => {
              setSelectedSessionId(event.target.value);
              void handleSelectSession(event.target.value);
            }}
          >
            <option value={sessionId}>Current · {currentSessionLabel}</option>
            {sessions
              .filter((session) => session.session_id !== sessionId)
              .map((session) => (
                <option key={session.session_id} value={session.session_id}>
                  {session.session_id.slice(0, 8)} · {session.message_count} msg · {session.preview.slice(0, 42)}
                </option>
              ))}
          </select>
        </label>
        <div className="session-actions">
          <button disabled={isStreaming} type="button" className="secondary" onClick={startNewSession}>
            New chat
          </button>
          <button
            disabled={isStreaming || messages.length === 0}
            type="button"
            className="secondary danger"
            onClick={handleClearCurrentSession}
          >
            Clear current
          </button>
        </div>
      </div>

      <div className="messages" aria-live="polite">
        {messages.length === 0 ? (
          <p className="empty">Send a message to verify token streaming.</p>
        ) : (
          messages.map((item, index) => (
            <article className={`message ${item.role}`} key={`${item.role}-${index}`}>
              <strong>{item.role === "user" ? "You" : "Cosmic Agent"}</strong>
              <p>{item.content || "…"}</p>
            </article>
          ))
        )}
      </div>

      <form className="chat-form" onSubmit={handleSubmit}>
        <div className="inline-fields">
          <label>
            Provider
            <input
              placeholder="google"
              value={provider}
              onChange={(event) => setProvider(event.target.value)}
            />
          </label>
          <label>
            Model
            <input
              placeholder="gemma-4-31b-it"
              value={model}
              onChange={(event) => setModel(event.target.value)}
            />
          </label>
        </div>
        <label className="checkbox-row">
          <input
            checked={parseCgi}
            type="checkbox"
            onChange={(event) => setParseCgi(event.target.checked)}
          />
          Queue CGI parse after stream
        </label>
        <textarea
          placeholder="Ask Cosmic Agent..."
          rows={4}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
        />
        <div className="actions">
          <button disabled={isStreaming || !message.trim()} type="submit">
            {isStreaming ? "Streaming..." : "Send"}
          </button>
          <button disabled={!isStreaming} type="button" className="secondary" onClick={stopStream}>
            Stop
          </button>
        </div>
      </form>
    </section>
  );
}

function JobMonitor() {
  const [jobs, setJobs] = useState<CGIParseJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("Waiting for refresh");

  const refreshJobs = useCallback(async () => {
    setLoading(true);
    try {
      const nextJobs = await fetchJobs();
      setJobs(nextJobs);
      setStatus(`Loaded ${nextJobs.length} jobs`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Failed to load jobs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshJobs();
    const timer = window.setInterval(refreshJobs, 5000);
    return () => window.clearInterval(timer);
  }, [refreshJobs]);

  async function handleRetryAll() {
    setLoading(true);
    try {
      const result = await retryAllJobs();
      setStatus(`Retry queued for ${result.reset_count} jobs`);
      await refreshJobs();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Retry failed");
    } finally {
      setLoading(false);
    }
  }

  const stoppedCount = jobs.filter((job) => job.status === "FAILED" || job.status === "QUOTA_LOCKED").length;

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Queue health</p>
          <h2>Job Monitor</h2>
        </div>
        <button disabled={loading || stoppedCount === 0} onClick={handleRetryAll} type="button">
          Retry All
        </button>
      </div>
      <p className="muted">{status}</p>
      <div className="job-list">
        {jobs.length === 0 ? (
          <p className="empty">No CGI parse jobs yet.</p>
        ) : (
          jobs.map((job) => <JobCard job={job} key={job.id} />)
        )}
      </div>
    </section>
  );
}

function JobCard({ job }: { job: CGIParseJob }) {
  return (
    <article className="job-card">
      <div className="job-card-top">
        <span className={`job-status ${job.status.toLowerCase()}`}>{job.status}</span>
        <code>{job.id.slice(0, 10)}</code>
      </div>
      <p>{job.user_message}</p>
      <dl>
        <div>
          <dt>Attempts</dt>
          <dd>
            {job.attempts}/{job.max_attempts}
          </dd>
        </div>
        <div>
          <dt>Error</dt>
          <dd>{job.last_error_type ?? "—"}</dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{job.updated_at}</dd>
        </div>
      </dl>
      {job.last_error_message ? <p className="error-text">{job.last_error_message}</p> : null}
    </article>
  );
}

function appendAssistantDelta(messages: ChatMessage[], delta: string): ChatMessage[] {
  const next = [...messages];
  const last = next[next.length - 1];
  if (!last || last.role !== "assistant") {
    next.push({ role: "assistant", content: delta });
    return next;
  }
  next[next.length - 1] = { ...last, content: last.content + delta };
  return next;
}

function getOrCreateSessionId(): string {
  const querySessionId = new URLSearchParams(window.location.search).get("session_id")?.trim();
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

function storeSessionId(sessionId: string): void {
  try {
    window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  } catch {
    // Storage can be unavailable in restricted browser contexts; the in-memory id still works.
  }
}

function createSessionId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }

  const randomValues = typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function"
    ? crypto.getRandomValues(new Uint8Array(16))
    : Uint8Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));

  randomValues[6] = (randomValues[6] & 0x0f) | 0x40;
  randomValues[8] = (randomValues[8] & 0x3f) | 0x80;

  const hex = [...randomValues].map((value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}
