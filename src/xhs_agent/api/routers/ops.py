"""运维路由分组（openapi tag：运维）。

S3.2 只建立分组；`GET /api/health` 的实现（DB / Redis / 模型三项探测）归 S3.3。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["运维"])
