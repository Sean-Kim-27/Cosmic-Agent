import {
  FormEvent,
  KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  clearChatHistory,
  fetchChatHistory,
  fetchChatSessions,
  streamChat,
} from "./api";
import {
  createSessionId,
  getOrCreateSessionId,
  storeSessionId,
} from "./sessionId";
import type { ChatMessage, ChatSessionSummary } from "./types";

export default function ChatPanel() {
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
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  const history = useMemo(
    () =>
      messages
        .filter(
          (item): item is Extract<ChatMessage, { role: "user" | "assistant" }> =>
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

  // Restore messages + CGI context whenever the active session changes.
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
        setHistoryStatus(
          error instanceof Error ? error.message : "Session list failed",
        );
      }
    });
    return () => {
      active = false;
    };
  }, [sessionId, refreshSessions]);

  // Auto-scroll to the bottom whenever the message stream grows.
  useEffect(() => {
    const target = messagesEndRef.current;
    if (!target) {
      return;
    }
    target.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

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
            setStatus(
              `Streaming from ${event.data.provider} · ${event.data.model}`,
            );
          } else if (event.event === "token") {
            setMessages((current) => appendAssistantDelta(current, event.data.text));
          } else if (event.event === "done") {
            setStatus(
              `Done · CGI parse ${event.data.parse_cgi ? "queued" : "disabled"}`,
            );
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

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter") {
      return;
    }
    // Plain Enter submits; Shift+Enter (and any modifier combo) inserts a newline
    // so the user can keep composing multi-line prompts without losing focus.
    if (event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    event.preventDefault();
    if (isStreaming) {
      return;
    }
    const form = event.currentTarget.form;
    if (form) {
      form.requestSubmit();
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
                  {session.session_id.slice(0, 8)} · {session.message_count} msg ·{" "}
                  {session.preview.slice(0, 42)}
                </option>
              ))}
          </select>
        </label>
        <div className="session-actions">
          <button
            disabled={isStreaming}
            type="button"
            className="secondary"
            onClick={startNewSession}
          >
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
          <p className="empty">
            Send a message to verify token streaming. Enter sends · Shift+Enter inserts a newline.
          </p>
        ) : (
          messages.map((item, index) => (
            <article
              className={`message ${item.role}`}
              key={`${item.role}-${index}`}
            >
              <strong>{item.role === "user" ? "You" : "Cosmic Agent"}</strong>
              <p>{item.content || "…"}</p>
            </article>
          ))
        )}
        <div ref={messagesEndRef} />
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
          aria-keyshortcuts="Enter Shift+Enter"
          placeholder="Ask Cosmic Agent...  (Enter to send · Shift+Enter for newline)"
          rows={4}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={handleComposerKeyDown}
        />
        <div className="actions">
          <button disabled={isStreaming || !message.trim()} type="submit">
            {isStreaming ? "Streaming..." : "Send"}
          </button>
          <button
            disabled={!isStreaming}
            type="button"
            className="secondary"
            onClick={stopStream}
          >
            Stop
          </button>
        </div>
      </form>
    </section>
  );
}

function appendAssistantDelta(
  messages: ChatMessage[],
  delta: string,
): ChatMessage[] {
  const next = [...messages];
  const last = next[next.length - 1];
  if (!last || last.role !== "assistant") {
    next.push({ role: "assistant", content: delta });
    return next;
  }
  next[next.length - 1] = { ...last, content: last.content + delta };
  return next;
}
