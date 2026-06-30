from __future__ import annotations

from app.core import (
    CGIMemoryWrite,
    LLMUsageWrite,
    SQLiteCGIMemoryStore,
    SQLiteUsageStore,
    estimate_text_tokens,
    estimate_usage_cost,
    parse_cgi_memory_document,
)


def test_usage_store_records_tokens_and_summarizes_today(tmp_path) -> None:
    store = SQLiteUsageStore(tmp_path / "usage.sqlite3")

    record = store.save(
        LLMUsageWrite(
            provider="google",
            model="gemma-test",
            operation="chat_stream",
            prompt_tokens=12,
            completion_tokens=3,
            estimated_cost_usd=0.000001,
            metadata={"token_source": "local_estimate"},
        )
    )
    summary = store.summarize_today()

    assert record.total_tokens == 15
    assert summary.calls == 1
    assert summary.prompt_tokens == 12
    assert summary.completion_tokens == 3
    assert summary.total_tokens == 15
    assert summary.estimated_cost_usd == 0.000001
    assert estimate_text_tokens("abcd") == 1
    assert (
        estimate_usage_cost(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            input_cost_per_million=0.1,
            output_cost_per_million=0.2,
        )
        == 0.3
    )


def test_escape_node_pruner_removes_low_weight_nodes_and_old_interactions(tmp_path) -> None:
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")
    for index in range(3):
        document = parse_cgi_memory_document(
            {
                "nodes": [
                    {
                        "label": f"Important {index}",
                        "summary": "Keep this node.",
                        "weight": 0.9,
                    },
                    {
                        "label": f"Garbage {index}",
                        "summary": "Prune this low-weight node.",
                        "weight": 0.01,
                    },
                ],
                "edges": [
                    {
                        "source_label": f"Important {index}",
                        "target_label": f"Garbage {index}",
                        "relation": "mentions",
                    }
                ],
            }
        )
        store.save(
            CGIMemoryWrite(
                session_id=f"session-{index}",
                user_message="noise",
                assistant_answer="noise",
                parser_provider="google",
                parser_model="gemma-test",
                document=document,
            )
        )

    result = store.prune(max_interactions=2, min_node_weight=0.05)
    events = store.list_pruning_events()

    assert result.strategy == "escape_node_pruner"
    assert result.before_interactions == 3
    assert result.after_interactions == 2
    assert result.before_nodes == 6
    assert result.after_nodes == 2
    assert result.pruned_interactions == 1
    assert result.pruned_nodes == 4
    assert result.pruned_edges == 3
    assert events[0].pruned_nodes == 4
