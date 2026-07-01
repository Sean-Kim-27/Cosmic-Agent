from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamStarted,
    AgentTextDelta,
    CGIParseJob,
)
from app.api.application import create_app
from app.api.dependencies import (
    get_agent_service,
    get_cgi_background_parser,
    get_cgi_memory_store,
    get_chat_history_store,
)
from app.core import ChatHistoryMessageWrite, SQLiteCGIMemoryStore, SQLiteChatHistoryStore


class FakeAgentService:
    async def stream_reply_events(self, request: AgentChatRequest) -> AsyncIterator[object]:
        assert request.message == "안녕?"
        yield AgentStreamStarted(provider="openai", model="gpt-stream-test")
        yield AgentTextDelta(text="안")
        yield AgentTextDelta(text="녕!")
        yield AgentStreamCompleted(provider="openai", model="gpt-stream-test")


class FakeCGIParser:
    def __init__(self) -> None:
        self.jobs: list[CGIParseJob] = []

    async def enqueue_and_process_safely(self, job: CGIParseJob) -> None:
        self.jobs.append(job)


def test_chat_stream_emits_sse_and_background_parser_receives_full_answer(
    tmp_path: Path,
) -> None:
    parser = FakeCGIParser()
    db_path = tmp_path / "memory.sqlite3"
    history_store = SQLiteChatHistoryStore(db_path)
    memory_store = SQLiteCGIMemoryStore(db_path)
    app = create_app()
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser
    app.dependency_overrides[get_chat_history_store] = lambda: history_store
    app.dependency_overrides[get_cgi_memory_store] = lambda: memory_store

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "안녕?", "session_id": "s1"},
        ) as response:
            body = response.read().decode("utf-8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: metadata\ndata: {"provider":"openai","model":"gpt-stream-test"}' in body
    assert 'event: token\ndata: {"text":"안"}' in body
    assert 'event: token\ndata: {"text":"녕!"}' in body
    assert (
        'event: done\ndata: {"provider":"openai","model":"gpt-stream-test","parse_cgi":true}'
        in body
    )
    assert parser.jobs == [
        CGIParseJob(
            session_id="s1",
            user_message="안녕?",
            assistant_answer="안녕!",
        )
    ]
    stored_messages = history_store.list_messages("s1")
    assert [message.role for message in stored_messages] == ["user", "assistant"]
    assert [message.content for message in stored_messages] == ["안녕?", "안녕!"]

    with TestClient(app) as client:
        history = client.get("/api/v1/chat/history/s1")

    assert history.status_code == 200
    assert [message["role"] for message in history.json()["messages"]] == ["user", "assistant"]


def test_chat_stream_skips_background_parse_on_stream_error(tmp_path: Path) -> None:
    parser = FakeCGIParser()
    history_store = SQLiteChatHistoryStore(tmp_path / "memory.sqlite3")

    class ErrorService:
        async def stream_reply_events(self, request: AgentChatRequest) -> AsyncIterator[object]:
            raise ValueError("missing provider")
            yield AgentTextDelta(text="")

    app = create_app()
    app.dependency_overrides[get_agent_service] = lambda: ErrorService()
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser
    app.dependency_overrides[get_chat_history_store] = lambda: history_store

    with TestClient(app) as client:
        response = client.post("/api/v1/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert 'event: error\ndata: {"code":"stream_error","message":"missing provider"}' in (
        response.text
    )
    assert parser.jobs == []
    assert history_store.list_messages("s1") == []


def test_chat_stream_can_disable_background_parse(tmp_path: Path) -> None:
    parser = FakeCGIParser()
    history_store = SQLiteChatHistoryStore(tmp_path / "memory.sqlite3")
    app = create_app()
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser
    app.dependency_overrides[get_chat_history_store] = lambda: history_store

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "안녕?", "parse_cgi": False},
        )

    assert response.status_code == 200
    assert (
        'event: done\ndata: {"provider":"openai","model":"gpt-stream-test","parse_cgi":false}'
    ) in response.text
    assert parser.jobs == []


def test_chat_sessions_can_be_listed_and_cleared(tmp_path: Path) -> None:
    history_store = SQLiteChatHistoryStore(tmp_path / "memory.sqlite3")
    history_store.save_message(ChatHistoryMessageWrite(session_id="old", role="user", content="첫 질문"))
    history_store.save_message(
        ChatHistoryMessageWrite(
            session_id="old",
            role="assistant",
            content="첫 답변",
            provider="nvidia",
            model="minimaxai/minimax-m3",
        )
    )
    history_store.save_message(ChatHistoryMessageWrite(session_id="new", role="user", content="새 질문"))
    app = create_app()
    app.dependency_overrides[get_chat_history_store] = lambda: history_store
    app.dependency_overrides[get_cgi_memory_store] = lambda: SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")

    with TestClient(app) as client:
        sessions = client.get("/api/v1/chat/sessions")
        cleared = client.delete("/api/v1/chat/history/old")
        old_history = client.get("/api/v1/chat/history/old")

    assert sessions.status_code == 200
    assert [session["session_id"] for session in sessions.json()["sessions"]] == ["new", "old"]
    assert sessions.json()["sessions"][0]["message_count"] == 1
    assert sessions.json()["sessions"][0]["preview"] == "새 질문"
    assert sessions.json()["sessions"][1]["message_count"] == 2
    assert sessions.json()["sessions"][1]["provider"] == "nvidia"
    assert sessions.json()["sessions"][1]["model"] == "minimaxai/minimax-m3"
    assert cleared.status_code == 200
    assert cleared.json() == {"session_id": "old", "deleted_messages": 2}
    assert old_history.json()["messages"] == []
