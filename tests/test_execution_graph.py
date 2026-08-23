"""Tests for durable execution-graph state and invariants."""

import sqlite3

import pytest

from execution_graph import (
    ExecutionGraphRepository,
    GraphNode,
    GraphRun,
    GraphStatus,
    InvalidTransition,
    NodeStatus,
    TerminationReason,
)


@pytest.fixture
def repository():
    conn = sqlite3.connect(":memory:")
    repo = ExecutionGraphRepository(conn)
    repo.ensure_schema()
    yield repo
    conn.close()


def _graph(graph_id="graph_test"):
    return GraphRun(
        id=graph_id,
        source="user",
        inputs={"query": "prepare my meeting"},
        metadata={"chat_id": 42},
        nodes=[
            GraphNode(
                id="calendar",
                action="calendar_search",
                inputs={"date": "tomorrow"},
                idempotency_key="calendar-search-tomorrow",
                max_attempts=2,
                timeout_seconds=30,
            ),
            GraphNode(
                id="brief",
                action="summarize",
                dependencies=["calendar"],
                inputs={"format": "brief"},
            ),
        ],
    )


def test_graph_rejects_missing_dependency_and_cycle():
    with pytest.raises(ValueError, match="missing dependencies"):
        GraphRun(
            source="user",
            nodes=[GraphNode(id="brief", action="summarize", dependencies=["missing"])],
        )

    with pytest.raises(ValueError, match="cycle"):
        GraphRun(
            source="user",
            nodes=[
                GraphNode(id="one", action="first", dependencies=["two"]),
                GraphNode(id="two", action="second", dependencies=["one"]),
            ],
        )


def test_graph_round_trips_typed_state(repository):
    created = repository.create_graph(_graph())
    loaded = repository.load_graph(created.id)

    assert loaded is not None
    assert loaded.status is GraphStatus.PENDING
    assert loaded.inputs == {"query": "prepare my meeting"}
    assert loaded.metadata == {"chat_id": 42}
    assert [node.id for node in loaded.nodes] == ["calendar", "brief"]
    assert loaded.nodes[0].status is NodeStatus.PENDING
    assert loaded.nodes[0].idempotency_key == "calendar-search-tomorrow"
    assert loaded.nodes[0].max_attempts == 2
    assert loaded.nodes[1].dependencies == ["calendar"]
    assert loaded.created_at is not None


def test_node_transitions_are_explicit_and_persisted(repository):
    repository.create_graph(_graph())

    with pytest.raises(InvalidTransition, match="pending to running"):
        repository.transition_node("graph_test", "calendar", NodeStatus.RUNNING)

    repository.transition_node("graph_test", "calendar", NodeStatus.READY)
    running = repository.transition_node("graph_test", "calendar", NodeStatus.RUNNING)
    succeeded = repository.transition_node("graph_test", "calendar", NodeStatus.SUCCEEDED)

    assert running.attempt_count == 1
    assert running.started_at is not None
    assert succeeded.status is NodeStatus.SUCCEEDED
    assert succeeded.completed_at is not None
    with pytest.raises(InvalidTransition, match="succeeded to running"):
        repository.transition_node("graph_test", "calendar", NodeStatus.RUNNING)


def test_checkpoint_persists_output_evidence_error_and_timestamp(repository):
    repository.create_graph(_graph())
    before = repository.load_graph("graph_test").updated_at

    node = repository.checkpoint_node(
        "graph_test",
        "calendar",
        outputs={"events": ["Planning review"]},
        evidence=["calendar event id evt_123"],
        error={"kind": "network", "retryable": True},
    )
    graph = repository.load_graph("graph_test")

    assert node.outputs == {"events": ["Planning review"]}
    assert node.evidence == ["calendar event id evt_123"]
    assert node.error == {"kind": "network", "retryable": True}
    assert node.updated_at is not None
    assert graph.updated_at >= before


def test_resumable_query_excludes_terminal_graphs(repository):
    repository.create_graph(_graph("graph_pending"))
    repository.create_graph(_graph("graph_done"))
    repository.transition_graph("graph_done", GraphStatus.RUNNING)
    completed = repository.transition_graph("graph_done", GraphStatus.SUCCEEDED)

    resumable = repository.list_resumable_runs()

    assert [graph.id for graph in resumable] == ["graph_pending"]
    assert completed.termination_reason is TerminationReason.SUCCEEDED
    assert completed.completed_at is not None
