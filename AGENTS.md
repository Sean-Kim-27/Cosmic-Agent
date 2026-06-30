# Cosmic Agent Development Instructions

이 파일은 CGI와 My-Agent를 통합하는 새 프로젝트 루트의 최상위 규칙이다.
하위의 기존 저장소에 있는 지침보다 이 파일을 우선한다.

## Phase Gate

- Phase 1 구조 확립, Phase 2 동적 설정·Provider 팩토리, Phase 3 SSE·백그라운드 CGI 파싱,
  Phase 4 CLI·대시보드 API·CGI 노드 CRUD, Phase 5 문서화·도커화는 완료되었다.
- 현재 상태는 **마스터 플랜 완료**다.
- 새 기능, 외부 배포, 공개 인증/권한, CI/CD 구성은 별도 사용자 승인 후 진행한다.
- 한 Phase가 끝날 때마다 변경 사항, 검증 결과, 남은 위험을 사용자에게 보고한다.

## Canonical Project Root

통합 프로젝트의 기준 루트는 이 파일이 있는 디렉터리다.

```text
/
├── app/
│   ├── api/
│   ├── core/
│   ├── agent/
│   ├── config/
│   ├── auth/
│   └── interfaces/
├── dashboard/
├── AGENTS.md
├── pyproject.toml
└── docker-compose.yml
```

이 구조 밖에 임의의 구현 파일이나 새 최상위 디렉터리를 만들지 않는다.
이후 Phase에서 명시적으로 요구된 `README.md`, `.env.example`, `Dockerfile`만
해당 Phase가 승인된 뒤 루트에 추가할 수 있다. 테스트 디렉터리 등 새로운
최상위 경계가 필요하면 먼저 사용자 확인을 받는다.

현재의 `CGI/`와 `my-agent/`는 이관 원본이다. 통합 런타임이 참조하는 위치가
아니며, 단계별 이관이 끝날 때까지 수정하거나 삭제하지 않는다.

## Component Boundaries

`app/api`

- FastAPI 라우터, 요청/응답 스키마, SSE 엔드포인트만 둔다.
- CGI 계산, LLM SDK 호출, 설정 저장 로직을 직접 구현하지 않는다.

`app/core`

- CGI 그래프 엔진, 메모리 도메인 모델, JSON 구조화 파서만 둔다.
- FastAPI, Telegram, CLI, 대시보드, 특정 LLM SDK를 import하지 않는다.

`app/agent`

- 대화 추론 흐름, 페르소나, provider 추상화와 orchestration을 둔다.
- 구체 API 키 저장 방식이나 인터페이스별 입출력 형식을 소유하지 않는다.

`app/config`

- 환경변수, 시스템 프롬프트, 모델 선택, SQLite 기반 런타임 override를 둔다.
- 비밀값을 로그나 API 응답에 원문으로 노출하지 않는다.

`app/auth`

- 기존 Codex OAuth 세션 재사용과 인증 관련 어댑터만 둔다.
- `~/.codex/auth.json` 토큰을 애플리케이션 코드가 직접 파싱하거나 갱신하지 않는다.

`app/interfaces`

- Telegram, Rich CLI, Web 같은 입출력 어댑터만 둔다.
- 모든 인터페이스는 같은 agent application service를 호출한다.

`dashboard`

- 웹 대시보드 프론트엔드만 둔다.
- 백엔드 비즈니스 로직이나 비밀값을 포함하지 않는다.

## Dependency Direction

의존성은 바깥 어댑터에서 안쪽 도메인으로만 흐른다.

```text
api / interfaces -> agent -> core
                       |
                       +-> config
                       +-> auth
```

- `core`는 다른 `app` 계층을 import하지 않는다.
- `agent`는 `api` 또는 `interfaces`를 import하지 않는다.
- provider SDK 객체는 `agent`의 protocol 뒤에 숨기고 생성 시 주입한다.
- 백그라운드 CGI 파싱은 스트리밍 응답 경로와 독립된 service boundary를 사용한다.

## Migration Map

- CGI `app/core/*` -> `app/core/`
- CGI 스키마 중 그래프 도메인 -> `app/core/`
- CGI/My-Agent LLM 호출 및 persona -> `app/agent/`
- CGI/My-Agent 환경 설정 -> `app/config/`
- My-Agent Codex runtime 인증 경계 -> `app/auth/`
- My-Agent Telegram bot -> `app/interfaces/`
- FastAPI 라우터 -> `app/api/`

파일을 단순 복사하지 말고, 각 Phase에서 테스트 가능한 작은 단위로 이관한다.
레거시 모듈을 새 계층에서 직접 import해 임시 결합을 만들지 않는다.

## Verification

Python 파일을 변경하면 최소한 다음을 실행한다.

```bash
PYTHONPYCACHEPREFIX=/private/tmp/cosmic-agent-pycache python3 -m compileall app
```

작업을 마치면 Obsidian vault의 `codex/YYYY-MM-DD.md`에 변경 요약,
검증 명령, 남은 위험을 기록한다.
