# Cosmic Agent (v1.0.0) 🚀

[![CI Pipeline](https://github.com/sean-kim-27/cosmic-agent/actions/workflows/main.yml/badge.svg)](https://github.com/sean-kim-27/cosmic-agent/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker Image](https://img.shields.io/badge/docker-ready-blue.svg)](https://hub.docker.com/)

> [🌐 View English Version README](./README.md)

**Cosmic Agent**는 고성능 오픈소스 자율형 AI 에이전트 시스템입니다. 실시간 토큰 스트리밍과 무거운 비동기 그래프 메모리 추출 작업(CGI - Cosmic Graph Intelligence)을 완벽하게 분리하여, 기존의 단순한 챗봇 경험을 스스로 진화하는 영속적 인지 에이전트 시스템으로 업그레이드합니다.

Vite + React 웹 대시보드, Telegram polling bot, Rich CLI 터미널 인터페이스를 모두 지원하며, 멀티 LLM 클라이언트 동적 라우팅, SQLite 기반 작업 큐 백엔드, API 과금 보호를 위한 스마트 지수 백오프 재시도 시스템, 그리고 MCP(Model Context Protocol) 도구 오케스트레이션을 완벽하게 결합하여 프로덕션 레벨의 인프라를 제공합니다.

---

## 🌌 시스템 아키텍처 및 데이터 흐름

Cosmic Agent는 사용자의 실시간 응답 지연을 방지하기 위해, 응답 생성과 CGI 그래프 메모리 업데이트 작업을 원자적(Atomic) SQLite 작업 큐를 활용하여 완벽한 투트랙(Two-Track) 비동기 파이프라인으로 처리합니다.

```text
[사용자 입력] ──► [FastAPI / 대시보드 / Telegram / CLI 진입점]
                        │
                        ├──► (1) 동적 LLM 클라이언트 라우팅 (OpenAI/Anthropic/Gemini)
                        │         │
                        │         ├──► [MCP 오케스트레이션] 외부 도구 실행 및 연동 (Obsidian, DB 등)
                        │         │
                        │         └──► [실시간 SSE 스트림] 사용자 UI에 답변 즉시 출력
                        │
                        └──► (2) 스트리밍 완료 ──► 비동기 CGI 파싱 작업 큐(Enqueue) 처리
                                                          │
                                                [SQLite 작업 큐]
                                                          │ (원자적 작업 할당 및 워커 스레드)
                                                          ▼
                                            [CGI 백그라운드 파서 엔진]
                                                          │
                                            ┌─────────────┴─────────────┐
                                            ▼                           ▼
                                    [CGI 그래프 동기화]           [스마트 재시도 엔진]
                                (메모리 저장 및 노드 Pruning)   (일시적에러: 백오프 / 할당량초과: 잠금)
```

---

## ✨ 핵심 기능

- **⚡ 분리형 비동기 인지 파이프라인:** 고성능 실시간 Server-Sent Events(SSE) 스트리밍과 원자적 SQLite 작업 큐를 결합한 백그라운드 연산 분리 구조. 그래프 데이터베이스 마이그레이션 중에도 UI 블로킹이 발생하지 않습니다.
- **🛠️ 프로덕션 레벨 MCP 오케스트레이션:** 표준 STDIO 및 HTTP/SSE JSON-RPC 트랜스포트 레이어 탑재. 에이전트가 `tools/list`를 통해 외부 도구(Obsidian 등)의 기능을 탐색하고, 자율적으로 도구를 호출하며, 데이터를 `<mcp_context>` 블록으로 바인딩하여 LLM 프롬프트에 주입합니다.
- **🧠 CGI 그래프 엔진 및 자동 복구 (Pruning):** `PROCESSING` 상태에서 서버가 멈춘 좀비 작업을 주기적으로 감시하고 되살리는 Stale-Lock 복구 루프 구현. 대화 시퀀스가 꼬이지 않도록 `rowid` 기반의 정렬 메커니즘을 적용하고, `escape_node_pruner`를 통해 쓸모없는 노드를 쳐내어 토큰 비용을 최적화합니다.
- **🛡️ 스마트 할당량 보호 (Smart Retry):** API 에러 트레이스를 추적하여 `transient(일시적)`, `quota(할당량 초과)`, `permanent(영구적 에러)`로 정밀 분류합니다. 구글 무료 티어나 OpenAI의 429 할당량 초과가 감지되면 무지성 재시도를 멈추고 `QUOTA_LOCKED` 상태로 작업을 안전하게 보존하여 API 계정을 보호합니다.
- **🖥️ 멀티 인터페이스 완벽 지원:**
  - **Vite + React 대시보드:** 실시간 API 사용량 추적, 저장 세션 전환, 현재 세션 초기화, 딥링크 스레드 지원 대화 내역 복원, 백그라운드 작업 큐 관리용 UI 및 "일괄 재시도" 제어 패널 탑재.
  - **Telegram Polling Bot:** allowlist 기반 개인 Telegram 어댑터. 웹 대시보드와 동일한 `CosmicAgentService` 및 SQLite 대화 기록 저장소를 사용하며 `/new`, `/reset`/`/clear`, `/sessions`, `/use <session_id>`, `/status` 명령을 지원합니다.
  - **Rich CLI 모드:** 터미널 환경에서 가볍고 빠르게 스트리밍 응답을 받아볼 수 있는 셸 전용 인터페이스.
- **🔒 철저한 보안 레이어:** `X-Cosmic-API-Key` 및 Bearer 토큰 검증 시스템과 IP 기반 Rate Limit(분당 요청 제한) 미들웨어를 내장하여 퍼블릭 배포 시에도 안전하게 코어를 방어합니다.

---

## 📁 디렉토리 구조

```text
/project_root
  ├── /app
  │    ├── /api           # FastAPI 애플리케이션 (라우터, 의존성 주입, 보안, 호환성 레이어)
  │    ├── /core          # 핵심 연산 모듈 (CGI 그래프 스토어, SQLite 디비, MCP 클라이언트)
  │    ├── /agent         # LLM 오케스트레이터, 페르소나 매니저, 재시도 커널, 큐 워커)
  │    ├── /config        # Pydantic 기반 환경변수 스키마, SQLite 마이그레이션 매트릭스)
  │    └── /auth          # Codex OAuth 인증 및 보안 레이어
  ├── /dashboard          # Vite + React + TypeScript 단일 페이지 애플리케이션 (SPA)
  ├── /deploy/systemd     # 프로덕션형 서비스 실행을 위한 systemd unit 예시
  ├── .github/workflows   # GitHub Actions 자동화 파이프라인 (Lint, Format, Pytest)
  ├── pyproject.toml      # 의존성 및 패키지 관리 선언 파일
  └── docker-compose.yml  # 원클릭 멀티 컨테이너 오케스트레이션 템플릿
```

---

## 🚀 빠른 시작

### 1. 환경 변수 세팅
레포지토리를 클론하고 로컬 실행 환경을 위한 `.env` 설정을 만듭니다:

```bash
cp .env.example .env
```

`.env` 파일을 열어 사용할 LLM API 키와 보안 설정을 입력합니다:
```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AIzaSy...
FRONTEND_API_SECRET=사용할_대시보드_인증_비밀키_입력
API_RATE_LIMIT_PER_MINUTE=60

# 선택 사항: Telegram polling adapter
TELEGRAM_BOT_TOKEN=123456789:replace-me
TELEGRAM_ALLOWED_USER_IDS=123456789
TELEGRAM_MAX_RESPONSE_CHARS=3900
TELEGRAM_HISTORY_LIMIT=40
```

### 2. 도커 컴포즈 실행 (추천)
명령어 한 방으로 백엔드 API 서버와 리액트 웹 대시보드를 동시에 구동합니다:

```bash
docker compose up -d --build
```
- **FastAPI 백엔드 문서 (Swagger):** `http://localhost:8000/docs`
- **Vite React 대시보드 웹앱:** `http://localhost:15173/`

### 3. Telegram Polling 모드 실행
개인 Telegram 인터페이스를 사용하려면 `.env`에 `TELEGRAM_BOT_TOKEN`과 쉼표로 구분한 `TELEGRAM_ALLOWED_USER_IDS` allowlist를 설정한 뒤 실행합니다:

```bash
python -m app.main --mode telegram
# 또는 pip install -e . 이후
cosmic-agent-telegram
```

Telegram bot은 대시보드와 동일한 `CosmicAgentService` 및 SQLite history 경계를 재사용합니다. 지원 명령은 `/new`, `/reset`/`/clear`, `/sessions`, `/use <session_id>`, `/status`입니다. systemd 템플릿은 `deploy/systemd/cosmic-agent-telegram.service`에 있습니다.

### 4. 터미널 CLI 모드 실행
로컬 터미널 셸 환경에서 빠르게 에이전트와 대화하고 싶다면 가상환경 빌드 후 진입합니다:

```bash
# 가상환경 구축 및 패키지 설치
python -m venv .venv
source .venv/bin/activate
pip install -e .

# CLI 실행 (구글 제미나이/Gemma 예시)
python -m app.main --mode cli --provider google --model gemma-4-31b-it
```

---

## 🧪 자동화 테스트 검증

Cosmic Agent는 코드 수정 시 발생할 수 있는 부작용을 방지하기 위해 정밀한 단위 테스트 매트릭스를 포함하고 있습니다.

```bash
# 전체 테스트 코드 구동
python -m pytest -q
```

---

## 📝 라이선스

본 프로젝트는 MIT 라이선스에 따라 배포됩니다. 자세한 내용은 `LICENSE` 파일을 참조하세요.
