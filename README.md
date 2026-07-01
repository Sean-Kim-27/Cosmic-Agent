# Cosmic Agent (v1.0.0) 🚀

[![CI Pipeline](https://github.com/sean-kim-27/cosmic-agent/actions/workflows/main.yml/badge.svg)](https://github.com/sean-kim-27/cosmic-agent/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker Image](https://img.shields.io/badge/docker-ready-blue.svg)](https://hub.docker.com/)

> [🌐 한국어 버전 README 보기 (Korean Version)](./README.ko.md)

**Cosmic Agent** is a production-ready, highly extensible, autonomous AI Agent system. It transforms a standard LLM chat experience into a persistent, self-evolving database-driven cognitive agent by decoupling real-time token streaming from heavy asynchronous graph updates (CGI - Cosmic Graph Intelligence). 

Equipped with three interfaces (Vite+React Dashboard, Telegram polling bot & Rich CLI), multi-LLM dynamic client routing, robust SQLite-backed task queuing with smart exponential backoff retry for quota protection, and full Model Context Protocol (MCP) tool orchestration, Cosmic Agent brings enterprise-grade agility to your local or cloud machine.

---

## 🌌 Core Architecture & Data Flow

Cosmic Agent splits real-time cognitive responses from heavy graph memory extraction using a decoupled, asynchronous multi-worker queue topology.

```text
[User Input] ──► [FastAPI / Dashboard / Telegram / CLI Entrypoint]
                        │
                        ├──► (1) Dynamic LLM Client Request (OpenAI/Anthropic/Gemini)
                        │         │
                        │         ├──► [MCP Orchestration] List/Call External Tools (Obsidian, Database)
                        │         │
                        │         └──► [Real-time SSE Stream] Immediate Response to UI
                        │
                        └──► (2) Stream Complete ──► Enqueue Durable CGI Parsing Job
                                                          │
                                                [SQLite Task Queue]
                                                          │ (Atomic Claim / Worker Thread)
                                                          ▼
                                            [CGI Background Parser Engine]
                                                          │
                                            ┌─────────────┴─────────────┐
                                            ▼                           ▼
                                    [CGI Graph Sync]            [Smart Retry Engine]
                                (Memory Store & Pruning)   (Transient: Backoff / Quota: Lock)
```

---

## ✨ Features

- **⚡ Split Asynchronous Cognitive Pipeline:** High-performance real-time Server-Sent Events (SSE) streaming combined with deferred background extraction via an atomic SQLite task queue. Your UI never freezes waiting for structural JSON updates.
- **🛠️ Production-Grade MCP Integration:** Built-in STDIO and HTTP/SSE JSON-RPC transports. The agent dynamically queries available capabilities via `tools/list`, performs autonomous function calls, sanitizes payload names, and transparently wraps data into bounded `<mcp_context>` blocks.
- **🧠 Cosmic Graph Intelligence (CGI) & Pruning:** Self-managing memory network featuring stale-lock recovery loops, interaction sequence preservation with `rowid` sorting, and deterministic token conservation via `escape_node_pruner` and `blackhole_compressor`.
- **🛡️ Smart Rate Limit & Robust Quota Shaving:** Intelligent error classification (`transient`, `quota`, `permanent`). If a Google free-tier or OpenAI 429 quota is exhausted, the job gracefully stops and locks under `QUOTA_LOCKED` status instead of hammering the server, preserving bandwidth and API health.
- **🖥️ Multi-Interface Access:**
  - **Vite + React Dashboard:** Modern SPA tracking live usage metrics, real-time message bubbles, saved-session switching, current-session clearing, deep-linkable session histories, and a full graphical JSON Queue monitor with an instant "Retry All" button.
  - **Telegram Polling Bot:** Optional allowlisted Telegram adapter backed by the same `CosmicAgentService` and SQLite history store. Supports `/new`, `/reset`/`/clear`, `/sessions`, `/use <session_id>`, and `/status`.
  - **Rich CLI Terminal:** Command-line wrapper rendering high-performance local textual streaming.
- **🔒 Security Shielded:** Endpoint rate-limiting paired with header token validation (`X-Cosmic-API-Key` / Bearer token schema) avoiding local environment leaking.

---

## 📁 Directory Structure

```text
/project_root
  ├── /app
  │    ├── /api           # FastAPI application (endpoints, dependencies, compat layers, rate limiters)
  │    ├── /core          # Core Engines (CGI graph structures, SQLite memory, MCP Client, history managers)
  │    ├── /agent         # LLM orchestration, persona handlers, retry kernels, task workers)
  │    ├── /config        # Dynamic environment schemas, SQLite migration matrices, secret management)
  │    └── /auth          # Codex token validation & OAuth abstraction
  ├── /dashboard          # Vite + React + TypeScript single-page application dashboard
  ├── /deploy/systemd     # Example systemd units for production-style services
  ├── .github/workflows   # Continuous Integration pipeline (Linting, Formatting, Unit tests)
  ├── pyproject.toml      # Dependency declaration matrix
  └── docker-compose.yml  # Zero-config multi-container topology
```

---

## 🚀 Quick Start

### 1. Environment Configuration
Clone the repository and create your local environment runtime blueprint:

```bash
cp .env.example .env
```

Open `.env` and fill in your keys:
```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AIzaSy...
FRONTEND_API_SECRET=your_super_secure_secret_api_key_here
API_RATE_LIMIT_PER_MINUTE=60

# Optional Telegram polling adapter
TELEGRAM_BOT_TOKEN=123456789:replace-me
TELEGRAM_ALLOWED_USER_IDS=123456789
TELEGRAM_MAX_RESPONSE_CHARS=3900
TELEGRAM_HISTORY_LIMIT=40
```

### 2. Run with Docker Compose (Recommended)
Boot up both the Backend API server and the Frontend React Dashboard concurrently with a single terminal instruction:

```bash
docker compose up -d --build
```
- **FastAPI API Documentation (Swagger):** `http://localhost:8000/docs`
- **Vite React Dashboard:** `http://localhost:15173/`

### 3. Run Telegram Polling Mode
For a private Telegram interface, set `TELEGRAM_BOT_TOKEN` and the comma-separated `TELEGRAM_ALLOWED_USER_IDS` allowlist in `.env`, then run:

```bash
python -m app.main --mode telegram
# or, after pip install -e .
cosmic-agent-telegram
```

The bot reuses the same `CosmicAgentService` and SQLite history boundary as the dashboard. Telegram commands include `/new`, `/reset`/`/clear`, `/sessions`, `/use <session_id>`, and `/status`. A production-style unit template is available at `deploy/systemd/cosmic-agent-telegram.service`.

### 4. Run Rich CLI Terminal Mode
If you prefer running a direct textual interaction directly in your shell environment:

```bash
# Set up Python environment
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Launch interactive CLI
python -m app.main --mode cli --provider google --model gemma-4-31b-it
```

---

## 🧪 Verification & Automated Testing

Cosmic Agent guarantees stability with comprehensive unit and regression suites spanning state transitions, SSE boundaries, and token tracking.

```bash
# Run pytest test matrices
python -m pytest -q
```

---

## 📝 License

Distributed under the MIT License. See `LICENSE` for more information.
