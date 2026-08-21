import asyncio
import pytest
from typing import Any, Dict, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from app.graph.build import build_noop_graph
from app.graph.checkpointer import MongoCheckpointSaver
from app.config.settings import settings


class InMemoryCollection:
    """In-memory mock for Motor Collection to allow tests without external Mongo daemon."""

    def __init__(self):
        self.docs: Dict[str, Dict[str, Any]] = {}

    async def create_index(self, keys, **kwargs):
        pass

    async def find_one(self, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        thread_id = query.get("thread_id")
        checkpoint_id = query.get("checkpoint_id")
        matching = []
        for doc in self.docs.values():
            if doc.get("thread_id") == thread_id:
                if checkpoint_id and doc.get("checkpoint_id") != checkpoint_id:
                    continue
                matching.append(doc)
        matching.sort(key=lambda d: (d.get("created_at", ""), d.get("time_ns", 0)), reverse=True)
        return matching[0] if matching else None

    def find(self, query: Dict[str, Any]):
        results = [
            doc for doc in self.docs.values()
            if not query.get("thread_id") or doc.get("thread_id") == query.get("thread_id")
        ]
        results.sort(key=lambda d: (d.get("created_at", ""), d.get("time_ns", 0)), reverse=True)
        return AsyncCursor(results)

    async def update_one(self, filter_dict: Dict[str, Any], update_dict: Dict[str, Any], upsert: bool = False):
        doc_id = filter_dict.get("_id")
        if not doc_id:
            return
        if doc_id not in self.docs:
            self.docs[doc_id] = {"_id": doc_id}
        if "$set" in update_dict:
            self.docs[doc_id].update(update_dict["$set"])
        if "$push" in update_dict:
            for k, v in update_dict["$push"].items():
                if k not in self.docs[doc_id]:
                    self.docs[doc_id][k] = []
                if isinstance(v, dict) and "$each" in v:
                    self.docs[doc_id][k].extend(v["$each"])
                else:
                    self.docs[doc_id][k].append(v)


class AsyncCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *args, **kwargs):
        return self

    def limit(self, limit):
        self.docs = self.docs[:limit]
        return self

    async def to_list(self, length=None):
        return self.docs[:length] if length else self.docs

    def __aiter__(self):
        self._iter = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class MockDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name: str):
        if name not in self.collections:
            self.collections[name] = InMemoryCollection()
        return self.collections[name]


@pytest.mark.asyncio
async def test_checkpoint_interruption_and_resumption():
    # Setup checkpointer backed by in-memory mock DB for deterministic unit testing
    mock_db = MockDatabase()
    checkpointer = MongoCheckpointSaver(db=mock_db)
    await checkpointer.setup()

    # Compile graph with interrupt_after node_a to simulate process interruption
    graph_paused = build_noop_graph(checkpointer=checkpointer, interrupt_after=["node_a"])

    config = {"configurable": {"thread_id": "test_thread_001"}}
    initial_state = {"run_id": "run_test_001", "reflection_count": 0, "status": "running"}

    # Phase 1: Run graph, it will execute node_a and pause/interrupt
    res_phase1 = await graph_paused.ainvoke(initial_state, config=config)

    # Verify state after Node A has executed
    saved_tuple = await checkpointer.aget_tuple(config)
    assert saved_tuple is not None
    assert saved_tuple.checkpoint["channel_values"].get("reflection_count") == 1
    assert saved_tuple.checkpoint["channel_values"].get("status") == "running"

    # Phase 2: Simulate process restart by re-instantiating graph and reloading from checkpointer
    graph_resumed = build_noop_graph(checkpointer=checkpointer)

    # Resume execution passing None as input state (loads state from checkpoint)
    res_phase2 = await graph_resumed.ainvoke(None, config=config)

    # Confirm node B ran and produced the expected final state
    assert res_phase2.get("reflection_count") == 1
    assert res_phase2.get("status") == "completed"
