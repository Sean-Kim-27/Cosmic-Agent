import { useState } from "react";
import ChatPanel from "./ChatPanel";
import DashboardPanel from "./DashboardPanel";

type TabKey = "chat" | "dashboard";

const TABS: Array<{ key: TabKey; label: string; eyebrow: string }> = [
  { key: "chat", label: "Chat", eyebrow: "Live SSE" },
  { key: "dashboard", label: "Dashboard", eyebrow: "Visualization" },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<TabKey>("chat");

  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Cosmic Agent</p>
          <h1>Streaming chat + visualization</h1>
        </div>
        <p className="hero-copy">
          Minimal launch dashboard for testing live SSE responses, queue health
          and background parse analytics.
        </p>
      </header>
      <nav className="tab-bar" role="tablist" aria-label="Primary">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            role="tab"
            aria-selected={activeTab === tab.key}
            type="button"
            className={`tab-button ${activeTab === tab.key ? "active" : ""}`}
            onClick={() => setActiveTab(tab.key)}
          >
            <span className="tab-eyebrow">{tab.eyebrow}</span>
            <span className="tab-label">{tab.label}</span>
          </button>
        ))}
      </nav>
      <section className="grid">
        {activeTab === "chat" ? <ChatPanel /> : <DashboardPanel />}
      </section>
    </main>
  );
}
