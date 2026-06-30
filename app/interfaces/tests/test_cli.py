from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from rich.console import Console

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamStarted,
    AgentTextDelta,
    CGIParseJob,
)
from app.interfaces.cli import RichCLIAdapter


class FakeService:
    def __init__(self) -> None:
        self.requests: list[AgentChatRequest] = []

    async def stream_reply_events(self, request: AgentChatRequest) -> AsyncIterator[object]:
        self.requests.append(request)
        yield AgentStreamStarted(provider="openai", model="gpt-test")
        yield AgentTextDelta(text="Hello ")
        yield AgentTextDelta(text="[literal]")
        yield AgentStreamCompleted(provider="openai", model="gpt-test")


class FakeParser:
    def __init__(self) -> None:
        self.jobs: list[CGIParseJob] = []

    async def enqueue_and_process_safely(self, job: CGIParseJob) -> None:
        self.jobs.append(job)


@pytest.mark.asyncio
async def test_rich_cli_streams_text_and_schedules_cgi_parse() -> None:
    service = FakeService()
    parser = FakeParser()
    console = Console(record=True, force_terminal=False)
    adapter = RichCLIAdapter(
        service=service,
        cgi_parser=parser,
        console=console,
        provider="openai",
        model="gpt-test",
    )

    answer = await adapter.run_once("Hi")
    await adapter.wait_for_background_tasks()

    assert answer == "Hello [literal]"
    assert service.requests[0].provider == "openai"
    assert service.requests[0].model == "gpt-test"
    assert parser.jobs == [
        CGIParseJob(
            session_id=adapter.session_id,
            user_message="Hi",
            assistant_answer="Hello [literal]",
        )
    ]
    assert "[literal]" in console.export_text()
    assert adapter.history[-2].content == "Hi"
    assert adapter.history[-1].content == "Hello [literal]"


@pytest.mark.asyncio
async def test_rich_cli_can_disable_cgi_parse() -> None:
    service = FakeService()
    parser = FakeParser()
    adapter = RichCLIAdapter(
        service=service,
        cgi_parser=parser,
        console=Console(record=True),
        parse_cgi=False,
    )

    await adapter.run_once("Hi")
    await adapter.wait_for_background_tasks()

    assert parser.jobs == []
