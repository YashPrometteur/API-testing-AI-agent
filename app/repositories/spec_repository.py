from typing import Any, Dict, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.repositories.base import BaseRepository


class SpecRepository(BaseRepository):
    """Repository for managing OpenAPI specifications in MongoDB 'specs' collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        super().__init__(db["specs"])

    async def get_active_spec(self, spec_id: str) -> Optional[Dict[str, Any]]:
        return await self.collection.find_one({"_id": spec_id, "is_deleted": {"$ne": True}})
