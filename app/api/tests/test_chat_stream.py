from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamStarted,
    AgentTextDelta,
    CGIParseJob,
)
from app.api.application import create_app
from app.api.dependencies import get_agent_service, get_cgi_background_parser


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

    async def parse_and_store_safely(self, job: CGIParseJob) -> None:
        self.jobs.append(job)


def test_chat_stream_emits_sse_and_background_parser_receives_full_answer() -> None:
    parser = FakeCGIParser()
    app = create_app()
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser

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


def test_chat_stream_skips_background_parse_on_stream_error() -> None:
    parser = FakeCGIParser()

    class ErrorService:
        async def stream_reply_events(self, request: AgentChatRequest) -> AsyncIterator[object]:
            raise ValueError("missing provider")
            yield AgentTextDelta(text="")

    app = create_app()
    app.dependency_overrides[get_agent_service] = lambda: ErrorService()
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser

    with TestClient(app) as client:
        response = client.post("/api/v1/chat/stream", json={"message": "hello"})

    assert response.status_code == 200
    assert 'event: error\ndata: {"code":"stream_error","message":"missing provider"}' in (
        response.text
    )
    assert parser.jobs == []


def test_chat_stream_can_disable_background_parse() -> None:
    parser = FakeCGIParser()
    app = create_app()
    app.dependency_overrides[get_agent_service] = lambda: FakeAgentService()
    app.dependency_overrides[get_cgi_background_parser] = lambda: parser

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
