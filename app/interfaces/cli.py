"""Rich-powered CLI adapter for Cosmic Agent."""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass, field

from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt

from app.agent import (
    AgentChatRequest,
    AgentStreamCompleted,
    AgentStreamStarted,
    AgentTextDelta,
    CGIBackgroundParser,
    CGIParseJob,
    ChatMessage,
    CosmicAgentService,
)
from app.interfaces.wiring import get_agent_service, get_cgi_background_parser


@dataclass(slots=True)
class RichCLIAdapter:
    """Terminal shell that streams tokens through Rich."""

    service: CosmicAgentService
    cgi_parser: CGIBackgroundParser
    console: Console = field(default_factory=Console)
    provider: str | None = None
    model: str | None = None
    parse_cgi: bool = True
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    history: list[ChatMessage] = field(default_factory=list)
    _background_tasks: set[asyncio.Task[object]] = field(default_factory=set, init=False)

    async def run(self) -> None:
        """Start an interactive terminal loop."""

        self.console.print("[bold cyan]Cosmic Agent CLI[/bold cyan]")
        self.console.print("Type [bold]exit[/bold], [bold]quit[/bold], or press Ctrl+C to leave.")
        while True:
            try:
                user_message = await asyncio.to_thread(Prompt.ask, "[bold green]You[/bold green]")
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[dim]bye[/dim]")
                break

            normalized = user_message.strip()
            if not normalized:
                continue
            if normalized.lower() in {"exit", "quit", ":q"}:
                break
            await self.run_once(normalized)

        await self.wait_for_background_tasks()

    async def run_once(self, message: str) -> str:
        """Stream one answer and schedule post-stream CGI parsing."""

        self.console.print("[bold magenta]Agent[/bold magenta]: ", end="")
        chunks: list[str] = []
        completed = False
        async for event in self.service.stream_reply_events(
            AgentChatRequest(
                message=message,
                history=tuple(self.history),
                provider=self.provider,
                model=self.model,
            )
        ):
            if isinstance(event, AgentStreamStarted):
                self.console.print(
                    f"[dim]({escape(event.provider)} · {escape(event.model)})[/dim] ",
                    end="",
                )
            elif isinstance(event, AgentTextDelta):
                chunks.append(event.text)
                self.console.print(escape(event.text), end="")
            elif isinstance(event, AgentStreamCompleted):
                completed = True
        self.console.print()

        answer = "".join(chunks)
        if completed and answer.strip():
            self.history.append(ChatMessage("user", message))
            self.history.append(ChatMessage("assistant", answer))
            if self.parse_cgi:
                task = asyncio.create_task(
                    self.cgi_parser.parse_and_store_safely(
                        CGIParseJob(
                            session_id=self.session_id,
                            user_message=message,
                            assistant_answer=answer,
                        )
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
        return answer

    async def wait_for_background_tasks(self) -> None:
        """Wait for scheduled CGI parsing tasks before shutdown."""

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Cosmic Agent in Rich CLI mode.")
    parser.add_argument("--provider", help="Override LLM provider for this CLI session.")
    parser.add_argument("--model", help="Override model or configured model alias.")
    parser.add_argument(
        "--no-cgi-parse",
        action="store_true",
        help="Disable post-stream CGI memory parsing for this CLI session.",
    )
    return parser


async def amain(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    adapter = RichCLIAdapter(
        service=get_agent_service(),
        cgi_parser=get_cgi_background_parser(),
        provider=args.provider,
        model=args.model,
        parse_cgi=not args.no_cgi_parse,
    )
    await adapter.run()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(amain(argv))


if __name__ == "__main__":
    main()
