from fastapi import FastAPI

from powercopy.core.config import get_settings

app = FastAPI(title="PowerCopy V2", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.env,
        "llm_ready": settings.llm_ready,
    }


@app.get("/")
async def index() -> dict:
    return {
        "name": "PowerCopy V2",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
    }
