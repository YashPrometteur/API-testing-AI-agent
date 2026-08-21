from typing import Any, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.repositories.base import BaseRepository


class FindingRepository(BaseRepository):
    """Repository for managing findings in MongoDB 'findings' collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        super().__init__(db["findings"])

    async def insert_finding(self, data: Dict[str, Any]) -> str:
        """Insert finding mapping finding ID to _id."""
        doc = dict(data)
        if "id" in doc and "_id" not in doc:
            doc["_id"] = doc.pop("id")
        return await self.insert(doc)

    async def find_by_run_id(self, run_id: str) -> List[Dict[str, Any]]:
        """Get all findings for a run."""
        cursor = self.collection.find({"run_id": run_id})
        return await cursor.to_list(length=1000)

    async def find_pending_review_by_run(self, run_id: str) -> List[Dict[str, Any]]:
        """Get all findings that are in pending_review state for a run."""
        cursor = self.collection.find({"run_id": run_id, "guardrail_status": "pending_review"})
        return await cursor.to_list(length=1000)
