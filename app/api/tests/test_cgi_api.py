from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.application import create_app
from app.api.dependencies import get_cgi_memory_store
from app.core import CGIMemoryWrite, SQLiteCGIMemoryStore, parse_cgi_memory_document


def build_memory_store(tmp_path) -> SQLiteCGIMemoryStore:
    store = SQLiteCGIMemoryStore(tmp_path / "memory.sqlite3")
    store.save(
        CGIMemoryWrite(
            session_id="session-1",
            user_message="remember this",
            assistant_answer="stream first, parse later",
            parser_provider="openai",
            parser_model="gpt-4o-mini",
            document=parse_cgi_memory_document(
                {
                    "nodes": [
                        {
                            "label": "Streaming",
                            "kind": "project_state",
                            "summary": "Responses stream before CGI parsing.",
                        },
                        {
                            "label": "Memory",
                            "kind": "component",
                            "summary": "CGI nodes are editable.",
                        },
                    ],
                    "edges": [
                        {
                            "source_label": "Streaming",
                            "target_label": "Memory",
                            "relation": "feeds",
                        }
                    ],
                }
            ),
        )
    )
    return store


def test_cgi_tree_and_node_crud(tmp_path) -> None:
    store = build_memory_store(tmp_path)
    app = create_app()
    app.dependency_overrides[get_cgi_memory_store] = lambda: store

    with TestClient(app) as client:
        tree = client.get("/api/v1/cgi/tree")
        nodes = client.get("/api/v1/cgi/nodes")
        node_id = nodes.json()[0]["id"]
        patch = client.patch(
            f"/api/v1/cgi/nodes/{node_id}",
            json={"label": "Memory edited", "tags": ["Phase-4", "phase-4"]},
        )
        delete = client.delete(f"/api/v1/cgi/nodes/{node_id}")
        missing = client.get(f"/api/v1/cgi/nodes/{node_id}")

    assert tree.status_code == 200
    assert tree.json()["interactions"][0]["session_id"] == "session-1"
    assert len(tree.json()["interactions"][0]["nodes"]) == 2
    assert nodes.status_code == 200
    assert patch.status_code == 200
    assert patch.json()["label"] == "Memory edited"
    assert patch.json()["tags"] == ["phase-4"]
    assert delete.status_code == 204
    assert missing.status_code == 404


def test_cgi_patch_rejects_empty_payload(tmp_path) -> None:
    store = build_memory_store(tmp_path)
    node_id = store.list_nodes()[0].id
    app = create_app()
    app.dependency_overrides[get_cgi_memory_store] = lambda: store

    with TestClient(app) as client:
        response = client.patch(f"/api/v1/cgi/nodes/{node_id}", json={})

    assert response.status_code == 422
