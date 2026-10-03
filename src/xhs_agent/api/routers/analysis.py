"""分析路由分组（openapi tag：分析）。

S3.2 只建立分组；`POST /api/analyze` 与 `GET /api/jobs/{job_id}` 的实现归 S3.3。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["分析"])
