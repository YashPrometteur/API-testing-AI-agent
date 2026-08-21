from typing import Any, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorCollection


class BaseRepository:
    """Generic async CRUD repository for MongoDB Motor collections."""

    def __init__(self, collection: AsyncIOMotorCollection):
        self.collection = collection

    async def find_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({"_id": item_id})

    async def insert(self, data: Dict[str, Any]) -> str:
        if "_id" not in data:
            raise ValueError("Document must include _id field")
        await self.collection.insert_one(data)
        return data["_id"]

    async def update(self, item_id: str, update_data: Dict[str, Any]) -> bool:
        res = await self.collection.update_one(
            {"_id": item_id},
            {"$set": update_data}
        )
        return res.modified_count > 0

    async def delete(self, item_id: str) -> bool:
        res = await self.collection.delete_one({"_id": item_id})
        return res.deleted_count > 0

    async def find_all(self, query: Optional[Dict[str, Any]] = None, limit: int = 100) -> List[Dict[str, Any]]:
        cursor = self.collection.find(query or {}).limit(limit)
        return await cursor.to_list(length=limit)
