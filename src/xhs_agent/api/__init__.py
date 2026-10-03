"""HTTP 层：FastAPI 应用、路由与依赖注入（统一 `/api` 前缀，见 ADR 0009）。

依赖方向：`api/` 可以调 `services/`、`workflows/` 与 `core/`；它不实现业务逻辑，
业务编排放 `workflows/`，数据访问放 `services/` / `db/`。
"""
