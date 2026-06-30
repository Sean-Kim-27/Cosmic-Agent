"""Dashboard CGI memory tree and brain-surgery CRUD APIs."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.dependencies import get_cgi_memory_store
from app.api.schemas import (
    CGIEdgeResponse,
    CGIInteractionTreeResponse,
    CGIMemoryMaintenanceResponse,
    CGINodePatchRequest,
    CGINodeResponse,
    CGIPruningEventResponse,
    CGITreeResponse,
)
from app.core import CGIMemoryNode, CGIMemoryNodePatch, SQLiteCGIMemoryStore

router = APIRouter(prefix="/api/v1/cgi", tags=["cgi"])


@router.get("/tree", response_model=CGITreeResponse)
async def get_cgi_tree(
    limit_interactions: int = Query(default=50, ge=1, le=200),
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> CGITreeResponse:
    """Return recent CGI memory grouped as a dashboard-friendly JSON tree."""

    tree = await asyncio.to_thread(
        memory_store.get_tree,
        limit_interactions=limit_interactions,
    )
    return CGITreeResponse(
        interactions=[
            CGIInteractionTreeResponse(
                id=interaction.id,
                session_id=interaction.session_id,
                user_message=interaction.user_message,
                assistant_answer=interaction.assistant_answer,
                parser_provider=interaction.parser_provider,
                parser_model=interaction.parser_model,
                created_at=interaction.created_at,
                nodes=[_node_response(node) for node in interaction.nodes],
                edges=[
                    CGIEdgeResponse(
                        id=edge.id,
                        interaction_id=edge.interaction_id,
                        source_label=edge.source_label,
                        target_label=edge.target_label,
                        relation=edge.relation,
                        weight=edge.weight,
                        metadata=edge.metadata,
                        created_at=edge.created_at,
                    )
                    for edge in interaction.edges
                ],
            )
            for interaction in tree.interactions
        ]
    )


@router.get("/nodes", response_model=list[CGINodeResponse])
async def list_cgi_nodes(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> list[CGINodeResponse]:
    """List CGI nodes newest-first for editable dashboard tables."""

    nodes = await asyncio.to_thread(memory_store.list_nodes, limit=limit, offset=offset)
    return [_node_response(node) for node in nodes]


@router.get("/nodes/{node_id}", response_model=CGINodeResponse)
async def get_cgi_node(
    node_id: str,
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> CGINodeResponse:
    node = await asyncio.to_thread(memory_store.get_node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CGI node not found")
    return _node_response(node)


@router.patch("/nodes/{node_id}", response_model=CGINodeResponse)
async def update_cgi_node(
    node_id: str,
    payload: CGINodePatchRequest,
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> CGINodeResponse:
    node = await asyncio.to_thread(
        memory_store.update_node,
        node_id,
        CGIMemoryNodePatch(
            label=payload.label,
            kind=payload.kind,
            summary=payload.summary,
            weight=payload.weight,
            tags=tuple(payload.tags) if payload.tags is not None else None,
            metadata=payload.metadata,
        ),
    )
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CGI node not found")
    return _node_response(node)


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cgi_node(
    node_id: str,
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> None:
    deleted = await asyncio.to_thread(memory_store.delete_node, node_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CGI node not found")


@router.post("/prune", response_model=CGIMemoryMaintenanceResponse)
async def prune_cgi_memory(
    max_interactions: int = Query(default=200, ge=1, le=10_000),
    min_node_weight: float = Query(default=0.05, ge=0.0, le=1.0),
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> CGIMemoryMaintenanceResponse:
    """Run escape_node_pruner immediately for stress-test and dashboard controls."""

    result = await asyncio.to_thread(
        memory_store.prune,
        max_interactions=max_interactions,
        min_node_weight=min_node_weight,
        strategy="escape_node_pruner",
    )
    return CGIMemoryMaintenanceResponse(
        strategy=result.strategy,
        before_interactions=result.before_interactions,
        after_interactions=result.after_interactions,
        before_nodes=result.before_nodes,
        after_nodes=result.after_nodes,
        before_edges=result.before_edges,
        after_edges=result.after_edges,
        pruned_interactions=result.pruned_interactions,
        pruned_nodes=result.pruned_nodes,
        pruned_edges=result.pruned_edges,
    )


@router.get("/pruning-events", response_model=list[CGIPruningEventResponse])
async def list_cgi_pruning_events(
    limit: int = Query(default=50, ge=1, le=200),
    memory_store: SQLiteCGIMemoryStore = Depends(get_cgi_memory_store),
) -> list[CGIPruningEventResponse]:
    """Return recent CGI pruning/compaction events."""

    events = await asyncio.to_thread(memory_store.list_pruning_events, limit=limit)
    return [
        CGIPruningEventResponse(
            id=event.id,
            strategy=event.strategy,
            before_interactions=event.before_interactions,
            after_interactions=event.after_interactions,
            before_nodes=event.before_nodes,
            after_nodes=event.after_nodes,
            before_edges=event.before_edges,
            after_edges=event.after_edges,
            pruned_interactions=event.pruned_interactions,
            pruned_nodes=event.pruned_nodes,
            pruned_edges=event.pruned_edges,
            created_at=event.created_at,
        )
        for event in events
    ]


def _node_response(node: CGIMemoryNode) -> CGINodeResponse:
    return CGINodeResponse(
        id=node.id,
        interaction_id=node.interaction_id,
        label=node.label,
        kind=node.kind,
        summary=node.summary,
        weight=node.weight,
        tags=list(node.tags),
        metadata=node.metadata,
        created_at=node.created_at,
    )
