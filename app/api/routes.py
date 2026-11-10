"""HTTP and WebSocket routes.

Thin by design: parse input, call the runtime, stream events. All decisions live
in ``app.pipeline`` and ``app.detector``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.runtime import DEMO_AUDIO_DIR, UPLOAD_DIR, Runtime

log = logging.getLogger(__name__)

router = APIRouter()

SSE_HEARTBEAT_S = 15.0


def _runtime(request: Request) -> Runtime:
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime not initialised")
    return runtime


# -- pages ---------------------------------------------------------------
@router.get("/", include_in_schema=False)
async def index() -> Any:
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).resolve().parent.parent / "ui" / "index.html")


@router.get("/family", include_in_schema=False)
async def family_page() -> Any:
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).resolve().parent.parent / "ui" / "family.html")


# -- health --------------------------------------------------------------
@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    return {
        "ok": True,
        "stt_ready": runtime.stt_ready,
        "llm_ready": runtime.llm_ready,
        "gnani_keys": runtime.gnani_pool.snapshot() if runtime.gnani_pool else [],
        "groq_keys": runtime.groq_pool.snapshot() if runtime.groq_pool else [],
        "active_calls": len([s for s in runtime.sessions.values() if s.running]),
    }


# -- calls ---------------------------------------------------------------
@router.post("/api/calls/file")
async def start_file_call(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    runtime = _runtime(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="file has no name")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename).suffix or ".wav"
    target = UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}{suffix}"
    target.write_bytes(await file.read())

    try:
        session = runtime.start_file_call(target)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"call_id": session.call_id}


@router.post("/api/calls/demo/{name}")
async def start_demo_call(name: str, request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    path = DEMO_AUDIO_DIR / f"{name}.wav"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"demo audio not found: {path.name}")
    try:
        session = runtime.start_file_call(path)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"call_id": session.call_id}


@router.get("/api/calls/demo")
async def list_demo_calls() -> dict[str, Any]:
    if not DEMO_AUDIO_DIR.exists():
        return {"demos": []}
    return {"demos": sorted(p.stem for p in DEMO_AUDIO_DIR.glob("*.wav"))}


@router.post("/api/calls/mic")
async def start_mic_call(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    try:
        session, _ = runtime.start_push_call()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"call_id": session.call_id}


@router.post("/api/calls/{call_id}/stop")
async def stop_call(call_id: str, request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    stopped = await runtime.stop_call(call_id)
    return {"stopped": stopped}


@router.get("/api/calls")
async def list_calls(request: Request, limit: int = 50) -> dict[str, Any]:
    runtime = _runtime(request)
    return {"calls": await runtime.store.list_calls(limit=limit)}


@router.get("/api/calls/{call_id}")
async def get_call(call_id: str, request: Request) -> Any:
    runtime = _runtime(request)
    call = await runtime.store.get_call(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return call


# -- live events ---------------------------------------------------------
@router.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    runtime = _runtime(request)
    queue = runtime.bus.subscribe()

    async def generator() -> Any:
        try:
            yield _sse({"kind": "hello", "at": time.time(), "data": {}})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                yield _sse(event)
        finally:
            runtime.bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# -- microphone audio ----------------------------------------------------
@router.websocket("/ws/mic/{call_id}")
async def mic_socket(websocket: WebSocket, call_id: str) -> None:
    runtime: Runtime = websocket.app.state.runtime
    source = runtime.push_sources.get(call_id)
    if source is None:
        await websocket.close(code=4004, reason="unknown call")
        return

    await websocket.accept()
    try:
        while True:
            chunk = await websocket.receive_bytes()
            await source.push(chunk)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("mic socket error")
    finally:
        await source.close()
