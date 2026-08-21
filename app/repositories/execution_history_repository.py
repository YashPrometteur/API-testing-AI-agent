from typing import Any, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.repositories.base import BaseRepository
from app.utils.datetime import current_utc_iso


class ExecutionHistoryRepository(BaseRepository):
    """Repository for historical endpoint execution runs in MongoDB 'execution_history' collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        super().__init__(db["execution_history"])

    async def update_incremental(
        self,
        spec_id: str,
        endpoint_operation_id: str,
        run_id: str,
        pass_count: int,
        fail_count: int,
        flaky_count: int,
        total_executions: int
    ) -> None:
        """Incrementally update execution history for a spec/endpoint pair."""
        query = {"spec_id": spec_id, "endpoint_operation_id": endpoint_operation_id}
        doc = await self.collection.find_one(query)

        now = current_utc_iso()
        run_entry = {
            "run_id": run_id,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "flaky_count": flaky_count,
            "total_executions": total_executions,
            "executed_at": now
        }

        if doc:
            run_ids = doc.get("run_ids", [])
            if run_id not in run_ids:
                run_ids.append(run_id)

            runs_history = doc.get("runs_history", [])
            # Prevent duplicates if node retries or resumes
            runs_history = [r for r in runs_history if r.get("run_id") != run_id]
            runs_history.append(run_entry)

            total_exec = doc.get("total_executions", 0) + total_executions
            p_count = doc.get("pass_count", 0) + pass_count
            f_count = doc.get("fail_count", 0) + fail_count
            fl_count = doc.get("flaky_count", 0) + flaky_count

            flake_rate = fl_count / total_exec if total_exec > 0 else 0.0

            update_doc = {
                "run_ids": run_ids,
                "runs_history": runs_history,
                "total_executions": total_exec,
                "pass_count": p_count,
                "fail_count": f_count,
                "flaky_count": fl_count,
                "flake_rate": flake_rate,
                "last_updated": now
            }
            await self.collection.update_one(query, {"$set": update_doc})
        else:
            flake_rate = flaky_count / total_executions if total_executions > 0 else 0.0
            new_doc = {
                "spec_id": spec_id,
                "endpoint_operation_id": endpoint_operation_id,
                "run_ids": [run_id],
                "runs_history": [run_entry],
                "total_executions": total_executions,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "flaky_count": flaky_count,
                "flake_rate": flake_rate,
                "last_updated": now
            }
            await self.collection.insert_one(new_doc)

    async def find_by_spec(self, spec_id: str) -> List[Dict[str, Any]]:
        """Retrieve execution history for all endpoints belonging to the specified spec."""
        cursor = self.collection.find({"spec_id": spec_id})
        return await cursor.to_list(length=1000)
