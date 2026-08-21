from fastapi import WebSocket, APIRouter

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/runs/{run_id}")
async def websocket_endpoint(websocket: WebSocket, run_id: str):
    await websocket.accept()
    await websocket.send_json({"event": "connected", "run_id": run_id})
    await websocket.close()
