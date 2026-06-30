from __future__ import annotations

from pathlib import Path

from app.core import (
    CGIMemoryNodePatch,
    CGIMemoryWrite,
    SQLiteCGIMemoryStore,
    parse_cgi_memory_document,
)


def test_parse_cgi_document_trims_edges_to_kept_nodes() -> None:
    document = parse_cgi_memory_document(
        {
            "nodes": [
                {"label": "A", "summary": "First"},
                {"label": "B", "summary": "Second"},
            ],
            "edges": [
                {"source_label": "A", "target_label": "B", "relation": "supports"},
                {"source_label": "B", "target_label": "C", "relation": "mentions"},
            ],
        }
    ).limited(2)

    assert [node.label for node in document.nodes] == ["A", "B"]
    assert [(edge.source_label, edge.target_label) for edge in document.edges] == [("A", "B")]


def test_sqlite_cgi_memory_store_persists_nodes(tmp_path: Path) -> None:
    document = parse_cgi_memory_document(
        {
            "nodes": [
                {
                    "label": "Streaming",
                    "summary": "Text is streamed before CGI memory parsing.",
                    "tags": ["phase-3", "phase-3"],
                }
            ],
            "edges": [],
        }
    )
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")

    record = store.save(
        CGIMemoryWrite(
            session_id=None,
            user_message="Explain Phase 3",
            assistant_answer="Streaming first, background parsing after.",
            parser_provider="openai",
            parser_model="gpt-4o-mini",
            document=document,
        )
    )

    assert record.node_count == 1
    assert store.count_nodes() == 1


def test_sqlite_cgi_memory_store_tree_update_and_delete_node(tmp_path: Path) -> None:
    document = parse_cgi_memory_document(
        {
            "nodes": [
                {"label": "Old label", "summary": "Original summary"},
                {"label": "Neighbor", "summary": "Linked node"},
            ],
            "edges": [
                {
                    "source_label": "Old label",
                    "target_label": "Neighbor",
                    "relation": "supports",
                }
            ],
        }
    )
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")
    store.save(
        CGIMemoryWrite(
            session_id="session-1",
            user_message="remember",
            assistant_answer="answer",
            parser_provider="openai",
            parser_model="gpt-4o-mini",
            document=document,
        )
    )

    node = next(item for item in store.list_nodes() if item.label == "Old label")
    updated = store.update_node(
        node.id,
        patch=CGIMemoryNodePatch(label="New label", tags=("phase-4",)),
    )
    tree = store.get_tree()

    assert updated is not None
    assert updated.label == "New label"
    assert updated.tags == ("phase-4",)
    assert tree.interactions[0].edges[0].source_label == "New label"

    assert store.delete_node(updated.id) is True
    remaining_tree = store.get_tree()
    assert [node.label for node in remaining_tree.interactions[0].nodes] == ["Neighbor"]
    assert remaining_tree.interactions[0].edges == ()
