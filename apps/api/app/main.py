"""FastAPI 装配：CORS + 错误处理 + 路由 + 启动自检。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.errors import register_error_handlers
from app.routers import asr_source, conversions, files, jobs
from app.services import registry


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.ensure_dirs()
    init_db()
    registry.register_handlers()
    registry.self_check()  # 启动自检：registry 无悬空 handler_key
    yield


app = FastAPI(title="多模态文件转换 API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)
app.include_router(files.router)
app.include_router(conversions.router)
app.include_router(jobs.router)
app.include_router(asr_source.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
