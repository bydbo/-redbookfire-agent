"""路由分层：按 OpenAPI 契约的三个 tag 分组。

- `ops.py`：运维（`GET /api/health`）
- `analysis.py`：分析（`POST /api/analyze`、`GET /api/jobs/{job_id}`）
- `result.py`：结果（`GET /api/runs/{run_id}`、`GET /api/runs/{run_id}/report`）

S3.2 只建立分组与挂载点，五个接口的实现归 S3.3。
"""
