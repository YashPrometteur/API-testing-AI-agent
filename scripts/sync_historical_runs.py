import sys
sys.path.insert(0, ".")
import asyncio
from app.database import get_mongo_db

async def sync_runs():
    db = await get_mongo_db()
    target_run_ids = ["run_1686cc6c9840", "run_1f581f5811cd"]
    
    for rid in target_run_ids:
        run_doc = await db["runs"].find_one({"_id": rid})
        if not run_doc:
            print(f"Run {rid} not found in current MongoDB database.")
            continue
            
        report = run_doc.get("final_report")
        if not report:
            print(f"Run {rid} has no final_report.")
            continue
            
        # 1. Refresh findings
        updated_findings = []
        for f in report.get("findings", []):
            fid = f.get("id") or f.get("_id")
            db_finding = await db["findings"].find_one({"_id": fid})
            if db_finding:
                f["guardrail_status"] = db_finding.get("guardrail_status", f.get("guardrail_status"))
                f["reviewer"] = db_finding.get("reviewer", f.get("reviewer"))
                f["review_notes"] = db_finding.get("review_notes", f.get("review_notes"))
                f["reviewed_at"] = db_finding.get("reviewed_at", f.get("reviewed_at"))
            updated_findings.append(f)
        report["findings"] = updated_findings
        
        # 2. Refresh defects_created
        defects = await db["defects"].find({"run_id": rid}).to_list(100)
        report["summary"]["defects_created"] = [d["_id"] for d in defects if d.get("_id")]
        
        # 3. Update in MongoDB
        await db["runs"].update_one(
            {"_id": rid},
            {"$set": {"final_report": report}}
        )
        print(f"Successfully refreshed and updated final_report for {rid}!")
        print("  defects_created:", report["summary"]["defects_created"])
        for f in report["findings"]:
            print(f"  finding {f.get('id')}: status={f.get('guardrail_status')}")

if __name__ == "__main__":
    asyncio.run(sync_runs())
