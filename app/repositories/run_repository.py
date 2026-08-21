from typing import Any, Dict, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.repositories.base import BaseRepository


class RunRepository(BaseRepository):
    """Repository for managing test run executions in MongoDB 'runs' collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        super().__init__(db["runs"])

    async def update_status(self, run_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> bool:
        update_data = {"status": status}
        if extra:
            update_data.update(extra)
        return await self.update(run_id, update_data)
