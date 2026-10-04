"""Celery worker 的启动入口（S3.4b）。

用法：

    uv run celery -A xhs_agent.tasks.worker:app worker --loglevel=info

为什么单独一个模块：Celery CLI 要求 `-A` 指向一个**真实的 Celery 实例**，所以模块级 `app`
必须在导入期读配置；而 API 进程与单测都不该因为 import 就要求 `REDIS_URL`（S3.2 的
"导入期不读配置"）。于是把"读配置 + 注册任务"的副作用集中在入口模块，工厂留在
`celery_app.py` / `analysis.py`（两者都不读配置，可离线单测）。
"""

from __future__ import annotations

from celery import Celery

from ..config import load_config
from ..core.logging import configure_logging
from ..tools.vision import make_describer
from .analysis import register_analyze_task
from .celery_app import build_celery_app

config = load_config()
configure_logging(config.log_level, config.log_format)

app: Celery = build_celery_app(config)

# 视觉打标走能力裁剪：没有 ffmpeg / 没开 [vision] 时 make_describer 返回 None
analyze_run = register_analyze_task(app, config, vision=make_describer(config))


if __name__ == "__main__":   # pragma: no cover - 手工启动别名（等价于 celery ... worker）
    app.start()
