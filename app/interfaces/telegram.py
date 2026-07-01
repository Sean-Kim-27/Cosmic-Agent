"""Telegram polling adapter for Cosmic Agent."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import SecretStr
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.agent import AgentChatRequest, AgentStreamCompleted, AgentStreamStarted, AgentTextDelta, ChatMessage
from app.config import load_settings
from app.core import ChatHistoryMessageWrite, SQLiteChatHistoryStore
from app.interfaces import wiring

if TYPE_CHECKING:
    from app.agent import CosmicAgentService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TelegramAdapterSettings:
    """Runtime settings needed by the Telegram adapter."""

    token: SecretStr
    allowed_user_ids: frozenset[int]
    max_response_chars: int = 3900
    history_limit: int = 40

    @classmethod
    def from_environment(cls) -> "TelegramAdapterSettings":
        settings = load_settings()
        if settings.telegram_bot_token is None:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for Telegram mode")
        if not settings.telegram_allowed_user_ids:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS must include at least one Telegram user id")
        return cls(
            token=settings.telegram_bot_token,
            allowed_user_ids=frozenset(settings.telegram_allowed_user_ids),
            max_response_chars=settings.telegram_max_response_chars,
            history_limit=settings.telegram_history_limit,
        )


@dataclass(slots=True)
class TelegramCosmicAgentBot:
    """Single-user Telegram polling interface backed by CosmicAgentService."""

    settings: TelegramAdapterSettings
    service: "CosmicAgentService"
    history_store: SQLiteChatHistoryStore
    active_sessions: dict[int, str] = field(default_factory=dict)

    def build_application(self) -> Application:
        app = Application.builder().token(self.settings.token.get_secret_value()).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CommandHandler("new", self.new_chat))
        app.add_handler(CommandHandler("reset", self.clear_current))
        app.add_handler(CommandHandler("clear", self.clear_current))
        app.add_handler(CommandHandler("sessions", self.sessions))
        app.add_handler(CommandHandler("use", self.use_session))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.message))
        return app

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "Cosmic Agent Telegram 연결이 준비됐습니다. 메시지를 보내면 웹 대시보드와 같은 에이전트로 답합니다.\n"
            "명령: /new, /reset, /sessions, /use <session_id>, /status"
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "/new: 새 Telegram 세션 시작\n"
            "/reset 또는 /clear: 현재 세션 대화 기록 삭제\n"
            "/sessions: 최근 저장 세션 목록\n"
            "/use <session_id>: 저장 세션으로 전환\n"
            "/status: 현재 세션 확인"
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        session_id = self._session_id(update.effective_chat.id)
        await update.effective_message.reply_text(f"현재 Cosmic session_id: {session_id}")

    async def new_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        session_id = f"telegram:{update.effective_chat.id}:{uuid.uuid4().hex}"
        self.active_sessions[update.effective_chat.id] = session_id
        await update.effective_message.reply_text(f"새 Cosmic Telegram 세션을 시작합니다.\n{session_id}")

    async def clear_current(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        session_id = self._session_id(update.effective_chat.id)
        deleted = await asyncio.to_thread(self.history_store.clear_session, session_id)
        await update.effective_message.reply_text(
            f"현재 세션 기록을 삭제했습니다. session_id={session_id}, deleted={deleted}"
        )

    async def sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        prefix = f"telegram:{update.effective_chat.id}"
        summaries = await asyncio.to_thread(self.history_store.list_sessions, limit=50)
        matches = [session for session in summaries if session.session_id.startswith(prefix)][:10]
        if not matches:
            await update.effective_message.reply_text("저장된 Telegram 세션이 아직 없습니다.")
            return
        lines = ["최근 Telegram 세션:"]
        for session in matches:
            preview = session.preview.replace("\n", " ")[:80]
            lines.append(f"- {session.session_id} · {session.message_count} messages · {preview}")
        await update.effective_message.reply_text("\n".join(lines))

    async def use_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        args = getattr(context, "args", []) or []
        if not args:
            await update.effective_message.reply_text("사용법: /use <session_id>")
            return
        requested = args[0].strip()
        allowed_prefix = f"telegram:{update.effective_chat.id}"
        if not requested.startswith(allowed_prefix):
            await update.effective_message.reply_text("이 Telegram chat에 속한 session_id만 선택할 수 있습니다.")
            return
        self.active_sessions[update.effective_chat.id] = requested
        await update.effective_message.reply_text(f"세션을 전환했습니다.\n{requested}")

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            return
        chat_id = update.effective_chat.id
        session_id = self._session_id(chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        answer, provider, model = await self._run_agent(session_id, prompt)
        await asyncio.to_thread(
            self.history_store.save_message,
            ChatHistoryMessageWrite(session_id=session_id, role="user", content=prompt),
        )
        await asyncio.to_thread(
            self.history_store.save_message,
            ChatHistoryMessageWrite(
                session_id=session_id,
                role="assistant",
                content=answer,
                provider=provider,
                model=model,
            ),
        )
        for chunk in self._split_response(answer):
            await update.effective_message.reply_text(chunk)

    async def _run_agent(self, session_id: str, prompt: str) -> tuple[str, str | None, str | None]:
        history_rows = await asyncio.to_thread(
            self.history_store.list_messages,
            session_id,
            limit=self.settings.history_limit,
        )
        history = tuple(ChatMessage(row.role, row.content) for row in history_rows)
        chunks: list[str] = []
        provider: str | None = None
        model: str | None = None
        async for event in self.service.stream_reply_events(AgentChatRequest(message=prompt, history=history)):
            if isinstance(event, AgentStreamStarted):
                provider = event.provider
                model = event.model
            elif isinstance(event, AgentTextDelta):
                chunks.append(event.text)
            elif isinstance(event, AgentStreamCompleted):
                provider = event.provider
                model = event.model
        answer = "".join(chunks).strip()
        if not answer:
            raise RuntimeError("Cosmic Agent returned an empty Telegram response")
        return answer, provider, model

    async def _guard(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id in self.settings.allowed_user_ids:
            return True
        logger.warning("telegram_user_rejected", extra={"user_id": user_id})
        if update.effective_message is not None:
            await update.effective_message.reply_text("이 Telegram 사용자는 허용되지 않았습니다.")
        return False

    def _session_id(self, chat_id: int) -> str:
        return self.active_sessions.setdefault(chat_id, f"telegram:{chat_id}")

    def _split_response(self, text: str) -> list[str]:
        max_chars = self.settings.max_response_chars
        if len(text) <= max_chars:
            return [text]
        return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def build_bot() -> TelegramCosmicAgentBot:
    settings = TelegramAdapterSettings.from_environment()
    return TelegramCosmicAgentBot(
        settings=settings,
        service=wiring.get_agent_service(),
        history_store=wiring.get_chat_history_store(),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    bot = build_bot()
    bot.build_application().run_polling()


if __name__ == "__main__":
    main()
