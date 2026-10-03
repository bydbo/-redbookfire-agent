"""依赖注入：把配置加载收敛成一个可覆盖的依赖。

用途：接口层用 `Depends(get_config)` 拿配置；进程级缓存避免每个请求重读 TOML；
      首次加载时按 `log_level` 初始化日志。
输入：无（走 `load_config()` 的默认三层优先级）。
输出：`AppConfig`；测试里用 `app.dependency_overrides[get_config] = lambda: fake_cfg` 覆盖。

注意：导入本模块或 `api.main` 都**不会**读配置——配置只在首个请求（或显式调用）时加载，
启动前置检查（含 FastAPI lifespan）归 S3.8。
"""

from __future__ import annotations

from functools import lru_cache

from ..config import AppConfig, load_config
from ..core.logging import configure_logging


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """加载并缓存配置（进程级）；顺带把日志级别配好。"""
    cfg = load_config()
    configure_logging(cfg.log_level)
    return cfg
