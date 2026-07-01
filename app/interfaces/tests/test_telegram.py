from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.agent import AgentStreamCompleted, AgentStreamStarted, AgentTextDelta
from app.core import ChatHistoryMessageWrite, SQLiteChatHistoryStore
from app.interfaces.telegram import TelegramAdapterSettings, TelegramCosmicAgentBot


class FakeService:
    async def stream_reply_events(self, request):
        self.request = request
        yield AgentStreamStarted(provider="nvidia", model="minimaxai/minimax-m3")
        yield AgentTextDelta("안녕")
        yield AgentTextDelta("하세요")
        yield AgentStreamCompleted(provider="nvidia", model="minimaxai/minimax-m3")


def make_bot(tmp_path: Path) -> TelegramCosmicAgentBot:
    return TelegramCosmicAgentBot(
        settings=TelegramAdapterSettings(
            token=SecretStr("token"),
            allowed_user_ids=frozenset({123}),
            max_response_chars=5,
            history_limit=10,
        ),
        service=FakeService(),  # type: ignore[arg-type]
        history_store=SQLiteChatHistoryStore(tmp_path / "history.sqlite3"),
    )


@pytest.mark.asyncio
async def test_telegram_run_agent_loads_session_history(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    bot.history_store.save_message(ChatHistoryMessageWrite(session_id="telegram:1", role="user", content="이전 질문"))
    bot.history_store.save_message(ChatHistoryMessageWrite(session_id="telegram:1", role="assistant", content="이전 답"))

    answer, provider, model = await bot._run_agent("telegram:1", "새 질문")

    assert answer == "안녕하세요"
    assert provider == "nvidia"
    assert model == "minimaxai/minimax-m3"
    assert [message.content for message in bot.service.request.history] == ["이전 질문", "이전 답"]
    assert bot.service.request.message == "새 질문"


def test_telegram_session_ids_are_chat_scoped(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)

    assert bot._session_id(42) == "telegram:42"
    assert bot._session_id(42) == "telegram:42"
    assert bot._session_id(43) == "telegram:43"


def test_telegram_response_splitting_respects_limit(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)

    assert bot._split_response("123456789") == ["12345", "6789"]


def test_telegram_main_suppresses_http_client_info_logs(monkeypatch) -> None:
    from app.interfaces import telegram as telegram_module

    class FakeApplication:
        def run_polling(self) -> None:
            return None

    class FakeBot:
        def build_application(self) -> FakeApplication:
            return FakeApplication()

    monkeypatch.setattr(telegram_module, "build_bot", lambda: FakeBot())

    telegram_module.main()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
