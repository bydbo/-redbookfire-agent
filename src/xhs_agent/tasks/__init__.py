"""Celery 任务层：把 `POST /api/analyze` 投进来的 job 变成一次真实分析（S3.4b）。

本包的模块分工：
- `celery_app.py`：Celery 应用工厂（纯函数，导入期不读配置，可离线单测）；
- `analysis.py`  ：任务体与同步外壳（重试 / 失败路径），同样不读配置；
- `worker.py`    ：worker 启动入口（模块级 app，Celery CLI 用 `-A ...:app` 指向它）。
"""
