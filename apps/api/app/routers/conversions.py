"""GET /api/conversions/supported —— 查询可转换目标（由 registry 生成）。规格 §9.2。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.errors import ConversionError, ErrorCode
from app.services import registry

router = APIRouter(prefix="/api/conversions", tags=["conversions"])


@router.get("/supported")
def supported(source_ext: str = Query(...)) -> dict:
    ext = source_ext.lower()
    try:
        routes = registry.get_targets(ext)
    except ConversionError as e:
        if e.code == ErrorCode.UNSUPPORTED_FILE_TYPE:
            return {"source_ext": source_ext, "targets": []}
        raise
    return {"source_ext": source_ext, "targets": routes}
