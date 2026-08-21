import asyncio
import json
import logging
import os
from typing import Tuple, Any
from datetime import datetime
try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

try:
    import mongomock_motor
except ImportError:
    mongomock_motor = None

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

try:
    import fakeredis
    try:
        import fakeredis.aioredis
    except ImportError:
        pass
except ImportError:
    fakeredis = None

from app.config.settings import settings

logger = logging.getLogger("app.database")

_mongo_client = None
_redis_client = None
_is_mock_mongo = False
_is_mock_redis = False

DB_FILE = os.path.join(os.getcwd(), "chroma_db", "persistent_mongo_mock.json")


def _json_serializer(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    return str(obj)


async def save_mock_db_to_disk():
    """Save all MongoMock collection data to local JSON file on disk."""
    global _mongo_client, _is_mock_mongo
    if not _is_mock_mongo or _mongo_client is None:
        return

    try:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        saved_data = {}
        dbs = await _mongo_client.list_database_names()
        for db_name in dbs:
            if db_name in ("admin", "local", "config"):
                continue
            database = _mongo_client[db_name]
            saved_data[db_name] = {}
            cols = await database.list_collection_names()
            for col_name in cols:
                docs = await database[col_name].find().to_list(length=5000)
                saved_data[db_name][col_name] = docs

        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(saved_data, f, default=_json_serializer, indent=2)
        logger.debug("[DATABASE] Successfully saved in-memory MongoMock state to disk.")
    except Exception as e:
        logger.warning(f"[DATABASE] Failed to save MongoMock state to disk: {e}")


async def load_mock_db_from_disk():
    """Load saved MongoMock collection data from local JSON file on disk."""
    global _mongo_client, _is_mock_mongo
    if not _is_mock_mongo or _mongo_client is None:
        return

    if not os.path.exists(DB_FILE):
        return

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            saved_data = json.load(f)

        for db_name, collections in saved_data.items():
            database = _mongo_client[db_name]
            for col_name, docs in collections.items():
                if docs:
                    await database[col_name].delete_many({})
                    await database[col_name].insert_many(docs)
        logger.info(f"[DATABASE] Successfully reloaded persistent MongoMock data from '{DB_FILE}'.")
    except Exception as e:
        logger.warning(f"[DATABASE] Failed to reload MongoMock state from disk: {e}")


async def get_mongo_client():
    global _mongo_client, _is_mock_mongo
    try:
        curr_loop = asyncio.get_running_loop()
        if _mongo_client is not None:
            c_loop = None
            if hasattr(_mongo_client, "get_io_loop"):
                try:
                    c_loop = _mongo_client.get_io_loop()
                except Exception:
                    pass
            if c_loop is None and hasattr(_mongo_client, "delegate") and hasattr(_mongo_client.delegate, "_event_loop"):
                c_loop = _mongo_client.delegate._event_loop

            if c_loop is not None and (c_loop.is_closed() or c_loop is not curr_loop):
                _mongo_client = None
    except Exception:
        pass

    if _mongo_client is not None and not _is_mock_mongo:
        return _mongo_client

    try:
        real_client = AsyncIOMotorClient(
            settings.MONGO_URI,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            maxPoolSize=20,
        )
        target_db = real_client[settings.MONGO_DB_NAME]
        await asyncio.wait_for(target_db.command("ping"), timeout=10.0)
        _mongo_client = real_client
        _is_mock_mongo = False
        logger.info(f"Connected to live remote MongoDB database '{settings.MONGO_DB_NAME}'.")
        return _mongo_client
    except Exception as e:
        if _mongo_client is not None and _is_mock_mongo:
            asyncio.create_task(save_mock_db_to_disk())
            return _mongo_client

        logger.warning(f"MongoDB not available ({e}). Falling back to persistent MongoMock.")
        if mongomock_motor is not None and hasattr(mongomock_motor, "AsyncMongoMockClient"):
            _mongo_client = mongomock_motor.AsyncMongoMockClient()
        else:
            raise RuntimeError(f"Neither live MongoDB nor mongomock_motor is available. Error: {e}")
        _is_mock_mongo = True
        await load_mock_db_from_disk()

    return _mongo_client


async def get_mongo_db():
    client = await get_mongo_client()
    db = client[settings.MONGO_DB_NAME]
    return db


async def get_redis_client():
    global _redis_client, _is_mock_redis
    if _redis_client is not None:
        return _redis_client

    try:
        if aioredis is not None:
            real_redis = aioredis.from_url(settings.REDIS_URL, socket_timeout=1.0)
            await asyncio.wait_for(real_redis.ping(), timeout=1.0)
            _redis_client = real_redis
            _is_mock_redis = False
            logger.info("Connected to live Redis.")
            return _redis_client
    except Exception as e:
        logger.warning(f"Redis not available ({e}). Falling back to in-memory FakeRedis.")

    if fakeredis is not None:
        if hasattr(fakeredis, "aioredis") and hasattr(fakeredis.aioredis, "FakeRedis"):
            _redis_client = fakeredis.aioredis.FakeRedis()
        elif hasattr(fakeredis, "FakeRedis"):
            _redis_client = fakeredis.FakeRedis()
        _is_mock_redis = True
    else:
        logger.warning("fakeredis is not installed; in-memory redis mock unavailable.")
        _redis_client = None
        _is_mock_redis = False

    return _redis_client


async def check_db_health() -> Tuple[str, str, str]:
    """Returns (status, mongo_msg, redis_msg)"""
    client = await get_mongo_client()
    redis_c = await get_redis_client()

    mongo_msg = "ok (persistent in-memory fallback)" if _is_mock_mongo else "ok"
    redis_msg = "ok (in-memory mock)" if _is_mock_redis else "ok"

    return "ok", mongo_msg, redis_msg
