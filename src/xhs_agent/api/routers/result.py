"""结果路由分组（openapi tag：结果）。

S3.2 只建立分组；`GET /api/runs/{run_id}` 与 `GET /api/runs/{run_id}/report` 的实现归 S3.3。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["结果"])
