from typing import Any, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.repositories.base import BaseRepository


class TestCaseRepository(BaseRepository):
    """Repository for managing generated test cases in MongoDB 'test_cases' collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        super().__init__(db["test_cases"])

    async def get_regression_test_cases(
        self,
        spec_id: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Fetch prior test cases prioritized by flake_rate and past confirmed defects."""
        query: Dict[str, Any] = {}
        if spec_id:
            query["spec_id"] = spec_id

        cursor = self.collection.find(query).sort([
            ("execution_history.confirmed_defects", -1),
            ("execution_history.flake_rate", -1),
            ("created_at", -1)
        ]).limit(limit)

        return await cursor.to_list(length=limit)

    async def save_test_cases(
        self,
        spec_id: str,
        run_id: str,
        test_cases: List[Dict[str, Any]]
    ) -> int:
        """Persist generated test cases joined with spec_id and run_id for future regression runs."""
        if not test_cases:
            return 0
        from datetime import datetime, timezone
        from pymongo import UpdateOne
        now_iso = datetime.now(timezone.utc).isoformat()
        operations = []
        for tc in test_cases:
            tc_id = tc.get("id")
            if not tc_id:
                continue
            doc = dict(tc)
            doc["_id"] = tc_id
            doc["spec_id"] = spec_id
            doc["run_id"] = run_id
            doc["created_at"] = doc.get("created_at") or now_iso
            operations.append(
                UpdateOne(
                    {"_id": tc_id},
                    {"$set": doc},
                    upsert=True
                )
            )
        if operations:
            res = await self.collection.bulk_write(operations, ordered=False)
            return (res.upserted_count or 0) + (res.modified_count or 0)
        return 0
