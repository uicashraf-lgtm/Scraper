import asyncio
import random

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

_connections: dict[WebSocket, str] = {}


def _guest_name() -> str:
    return f"Guest#{random.randint(1000, 9999)}"


async def _broadcast(payload: dict, exclude: WebSocket = None):
    dead = set()
    for ws in list(_connections):
        if ws is exclude:
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _connections.pop(ws, None)


@router.websocket("/chat/ws")
async def chat_ws(websocket: WebSocket):
    await websocket.accept()
    name = _guest_name()
    _connections[websocket] = name

    try:
        await websocket.send_json({"type": "assigned_name", "name": name})
        await _broadcast({"type": "system", "text": f"{name} joined"}, exclude=websocket)

        async for data in websocket.iter_json():
            text = str(data.get("text", "")).strip()
            if text and len(text) <= 500:
                await _broadcast({"type": "message", "name": name, "text": text})

    except WebSocketDisconnect:
        pass
    finally:
        _connections.pop(websocket, None)
        await _broadcast({"type": "system", "text": f"{name} left"})
