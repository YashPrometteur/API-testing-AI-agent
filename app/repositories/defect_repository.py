from typing import Any, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.repositories.base import BaseRepository


class DefectRepository(BaseRepository):
    """Repository for managing approved defects in MongoDB 'defects' collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        super().__init__(db["defects"])

    async def insert_defect(self, data: Dict[str, Any]) -> str:
        """Insert defect mapping defect ID to _id."""
        doc = dict(data)
        if "id" in doc and "_id" not in doc:
            doc["_id"] = doc.pop("id")
        return await self.insert(doc)

    async def find_defects(
        self,
        run_id: Optional[str] = None,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        spec_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Find defects matching query filters with pagination."""
        query = {}
        if run_id:
            query["run_id"] = run_id
        if status:
            query["status"] = status
        if severity:
            query["severity"] = severity
        if spec_id:
            query["spec_id"] = spec_id

        cursor = self.collection.find(query).skip(skip).limit(limit)
        return await cursor.to_list(length=limit)
