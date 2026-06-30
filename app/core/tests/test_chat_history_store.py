from __future__ import annotations

from app.core import ChatHistoryMessageWrite, SQLiteChatHistoryStore


def test_chat_history_store_persists_session_messages_in_order(tmp_path) -> None:
    store = SQLiteChatHistoryStore(tmp_path / "memory.sqlite3")

    store.save_message(ChatHistoryMessageWrite("s1", "user", "hello"))
    store.save_message(
        ChatHistoryMessageWrite(
            session_id="s1",
            role="assistant",
            content="hi there",
            provider="google",
            model="gemma-test",
        )
    )
    store.save_message(ChatHistoryMessageWrite("s2", "user", "other"))

    messages = store.list_messages("s1")

    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == ["hello", "hi there"]
    assert messages[1].provider == "google"
    assert messages[1].model == "gemma-test"
