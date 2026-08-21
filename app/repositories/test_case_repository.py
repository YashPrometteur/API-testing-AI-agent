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
