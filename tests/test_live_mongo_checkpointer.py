import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from app.graph.build import build_noop_graph
from app.graph.checkpointer import MongoCheckpointSaver
from app.config.settings import settings


@pytest.mark.asyncio
async def test_live_mongo_checkpoint_interruption_and_resumption():
    from app.database import get_mongo_db
    db = await get_mongo_db()
    
    # Clean up test collection before test
    await db["checkpoints"].delete_many({"thread_id": "live_test_thread_001"})

    checkpointer = MongoCheckpointSaver(db=db)
    await checkpointer.setup()

    # Compile graph with interrupt_after node_a to simulate process interruption
    graph_paused = build_noop_graph(checkpointer=checkpointer, interrupt_after=["node_a"])

    config = {"configurable": {"thread_id": "live_test_thread_001"}}
    initial_state = {"run_id": "run_live_001", "reflection_count": 0, "status": "running"}

    # Phase 1: Run graph, it will execute node_a and interrupt
    res_phase1 = await graph_paused.ainvoke(initial_state, config=config)

    # Verify state in real MongoDB after Node A execution
    saved_doc = await db["checkpoints"].find_one({"thread_id": "live_test_thread_001", "state.reflection_count": 1})
    assert saved_doc is not None, "Checkpoint doc with reflection_count=1 must exist in real MongoDB collection"
    assert saved_doc["state"]["reflection_count"] == 1

    saved_tuple = await checkpointer.aget_tuple(config)
    assert saved_tuple is not None
    assert saved_tuple.checkpoint["channel_values"].get("reflection_count") == 1

    # Phase 2: Simulate process restart, re-instantiate graph & checkpointer, resume node_b
    graph_resumed = build_noop_graph(checkpointer=checkpointer)
    res_phase2 = await graph_resumed.ainvoke(None, config=config)

    # Confirm node B ran and produced the expected final state
    assert res_phase2.get("reflection_count") == 1
    assert res_phase2.get("status") == "completed"

    # Clean up test thread data
    await db["checkpoints"].delete_many({"thread_id": "live_test_thread_001"})
