import asyncio
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    ChannelVersions,
    SerializerProtocol,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langchain_core.runnables import RunnableConfig
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase


def make_bson_safe(val: Any) -> Any:
    """Recursively clean dict/list/values to ensure they only contain BSON-serializable primitives."""
    if isinstance(val, dict):
        return {str(k): make_bson_safe(v) for k, v in val.items() if isinstance(k, (str, int, float, bool, type(None)))}
    elif isinstance(val, list):
        return [make_bson_safe(v) for v in val]
    elif isinstance(val, tuple):
        return [make_bson_safe(v) for v in val]
    elif isinstance(val, (str, int, float, bool, type(None), datetime)):
        return val
    else:
        # Fallback to string representation to avoid any BSON encoding exceptions
        return str(val)


class MongoCheckpointSaver(BaseCheckpointSaver):
    """
    Custom MongoDB-backed CheckpointSaver for LangGraph using Motor (async).
    Schema in 'checkpoints' collection:
    - _id: str (composite: f"{thread_id}_{checkpoint_id}")
    - thread_id: str
    - checkpoint_id: str
    - parent_checkpoint_id: Optional[str]
    - state: Dict (channel_values / serialized TestRunState)
    - metadata: Dict (node, step, etc.)
    - created_at: str (ISO format)
    """

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        collection_name: str = "checkpoints",
        serde: Optional[SerializerProtocol] = None,
    ):
        super().__init__(serde=serde or JsonPlusSerializer())
        self.db = db
        self.collection: AsyncIOMotorCollection = db[collection_name]

    async def setup(self) -> None:
        """Create required index on {thread_id: 1, created_at: -1}."""
        await self.collection.create_index([("thread_id", 1), ("created_at", -1)])

    def _doc_to_checkpoint_tuple(self, doc: Dict[str, Any]) -> CheckpointTuple:
        thread_id = doc["thread_id"]
        checkpoint_id = doc["checkpoint_id"]
        parent_id = doc.get("parent_checkpoint_id")

        config: RunnableConfig = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id,
            }
        }
        if doc.get("checkpoint_ns"):
            config["configurable"]["checkpoint_ns"] = doc["checkpoint_ns"]

        parent_config: Optional[RunnableConfig] = None
        if parent_id:
            parent_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": parent_id,
                }
            }
            if doc.get("checkpoint_ns"):
                parent_config["configurable"]["checkpoint_ns"] = doc["checkpoint_ns"]

        # 1. Deserialize checkpoint_data
        checkpoint_serialized = doc.get("checkpoint_data_serialized")
        if checkpoint_serialized:
            checkpoint: Checkpoint = self.serde.loads_typed((
                checkpoint_serialized["type"],
                checkpoint_serialized["bytes"]
            ))
        else:
            checkpoint: Checkpoint = doc.get("checkpoint_data") or {
                "v": 1,
                "id": checkpoint_id,
                "ts": doc.get("created_at", datetime.now(timezone.utc).isoformat()),
                "channel_values": doc.get("state", {}),
                "channel_versions": doc.get("channel_versions", {}),
                "versions_seen": doc.get("versions_seen", {}),
                "pending_sends": doc.get("pending_sends", []),
            }

        # 2. Deserialize metadata
        metadata_serialized = doc.get("metadata_serialized")
        if metadata_serialized:
            metadata: CheckpointMetadata = self.serde.loads_typed((
                metadata_serialized["type"],
                metadata_serialized["bytes"]
            ))
        else:
            metadata: CheckpointMetadata = doc.get("metadata", {})

        # 3. Deserialize pending_writes
        pending_writes = []
        for w in doc.get("pending_writes", []):
            if isinstance(w, dict) and "type" in w and "bytes" in w:
                val = self.serde.loads_typed((w["type"], w["bytes"]))
                task_id = w.get("task_id", "")
                channel = w.get("channel", "")
                pending_writes.append((task_id, channel, val))
            elif isinstance(w, (list, tuple)) and len(w) == 3:
                pending_writes.append((w[0], w[1], w[2]))
            elif isinstance(w, (list, tuple)) and len(w) == 2:
                # Legacy 2-tuple format: backfill task_id as empty string
                pending_writes.append(("", w[0], w[1]))

        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    def _run_async(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # If called inside an existing running event loop (e.g. FastAPI / LangGraph async thread)
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(coro)
        else:
            return asyncio.run(coro)

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                # In async context, aget_tuple should be used by LangGraph
                fut = asyncio.run_coroutine_threadsafe(self.aget_tuple(config), loop)
                return fut.result(timeout=10.0)
        except Exception:
            pass
        return self._run_async(self.aget_tuple(config))

    def list(
        self,
        config: Optional[RunnableConfig] = None,
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> List[CheckpointTuple]:
        async def _collect():
            return [c async for c in self.alist(config, filter=filter, before=before, limit=limit)]
        return self._run_async(_collect())

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(self.aput(config, checkpoint, metadata, new_versions), loop)
                return fut.result(timeout=10.0)
        except Exception:
            pass
        return self._run_async(self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(self.aput_writes(config, writes, task_id), loop)
                return fut.result(timeout=10.0)
        except Exception:
            pass
        self._run_async(self.aput_writes(config, writes, task_id))

    # --- Async Interface Implementations ---

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"].get("checkpoint_id")
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")

        query: Dict[str, Any] = {"thread_id": thread_id}
        if checkpoint_ns:
            query["checkpoint_ns"] = checkpoint_ns
        if checkpoint_id:
            query["checkpoint_id"] = checkpoint_id

        if checkpoint_id:
            doc = await self.collection.find_one(query)
        else:
            cursor = self.collection.find(query).sort("created_at", -1).limit(1)
            docs = await cursor.to_list(length=1)
            doc = docs[0] if docs else None

        if not doc:
            return None

        return self._doc_to_checkpoint_tuple(doc)

    async def alist(
        self,
        config: Optional[RunnableConfig] = None,
        *,
        filter: Optional[Dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[CheckpointTuple]:
        query: Dict[str, Any] = {}
        if config and "configurable" in config:
            if "thread_id" in config["configurable"]:
                query["thread_id"] = config["configurable"]["thread_id"]
            if "checkpoint_ns" in config["configurable"] and config["configurable"]["checkpoint_ns"]:
                query["checkpoint_ns"] = config["configurable"]["checkpoint_ns"]

        if filter:
            query.update(filter)

        cursor = self.collection.find(query).sort("created_at", -1)
        if limit:
            cursor = cursor.limit(limit)

        async for doc in cursor:
            yield self._doc_to_checkpoint_tuple(doc)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = checkpoint["id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        parent_id = config["configurable"].get("checkpoint_id")

        doc_id = f"{thread_id}_{checkpoint_ns}_{checkpoint_id}" if checkpoint_ns else f"{thread_id}_{checkpoint_id}"
        created_at = datetime.now(timezone.utc).isoformat()
        time_ns = time.time_ns()

        t_checkpoint, b_checkpoint = self.serde.dumps_typed(checkpoint)
        t_metadata, b_metadata = self.serde.dumps_typed(metadata)

        doc = {
            "_id": doc_id,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": parent_id,
            "state": make_bson_safe(checkpoint.get("channel_values", {})),
            "metadata": make_bson_safe(metadata) if metadata else {},
            "checkpoint_data_serialized": {
                "type": t_checkpoint,
                "bytes": b_checkpoint,
            },
            "metadata_serialized": {
                "type": t_metadata,
                "bytes": b_metadata,
            },
            "created_at": created_at,
            "time_ns": time_ns,
        }

        await self.collection.update_one(
            {"_id": doc_id},
            {"$set": doc},
            upsert=True,
        )

        res_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id,
            }
        }
        if checkpoint_ns:
            res_config["configurable"]["checkpoint_ns"] = checkpoint_ns
        return res_config

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[Tuple[str, Any]],
        task_id: str,
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = config["configurable"]["checkpoint_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        doc_id = f"{thread_id}_{checkpoint_ns}_{checkpoint_id}" if checkpoint_ns else f"{thread_id}_{checkpoint_id}"

        formatted_writes = []
        for write_tuple in writes:
            # LangGraph 1.2: writes are (channel, value) 2-tuples; task_id is the separate param
            # LangGraph may also pass (task_id, channel, value) 3-tuples in some contexts
            if len(write_tuple) == 3:
                w_task_id, channel, val = write_tuple
            else:
                channel, val = write_tuple
                w_task_id = task_id
            t, b = self.serde.dumps_typed(val)
            formatted_writes.append({
                "task_id": w_task_id,
                "channel": channel,
                "type": t,
                "bytes": b,
            })

        await self.collection.update_one(
            {"_id": doc_id},
            {"$push": {"pending_writes": {"$each": formatted_writes}}},
        )
