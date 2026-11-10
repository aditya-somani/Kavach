"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.runtime import build_runtime
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("kavach")

UI_DIR = Path(__file__).resolve().parent / "ui"
AUDIO_DIR = settings.db_path.parent / "audio"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("starting Kavach")
    app.state.runtime = build_runtime(settings)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        log.info("shutting down Kavach")
        await app.state.runtime.shutdown()


app = FastAPI(title="Kavach", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=UI_DIR, check_dir=False), name="static")
app.mount("/audio", StaticFiles(directory=AUDIO_DIR, check_dir=False), name="audio")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
