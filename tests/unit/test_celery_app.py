"""Celery 应用工厂与任务外壳的单测（S3.4b）：全离线，不连 Redis、不连库。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded
from sqlalchemy.exc import InterfaceError, OperationalError

from xhs_agent.config import AppConfig, ConfigError, EnvView, load_config
from xhs_agent.schemas import SchemaError
from xhs_agent.tasks import analysis
from xhs_agent.tasks import reconcile as reconcile_task
from xhs_agent.tasks.analysis import (
    ANALYZE_TASK_NAME,
    TRANSIENT_ERRORS,
    execute,
    register_analyze_task,
)
from xhs_agent.tasks.celery_app import (
    RETRY_POLICY,
    SOCKET_TIMEOUT_S,
    build_celery_app,
    install_logging,
    redis_url,
)
from xhs_agent.tasks.reconcile import install_worker_ready_reconcile, stale_threshold_s
from xhs_agent.tools.embedding import EmbeddingError
from xhs_agent.tools.llm import LLMError


def make_config(tmp_path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(str(path))


def with_redis(cfg: AppConfig, url: str) -> AppConfig:
    """只改配置快照里的 REDIS_URL——用例绝不碰开发者本机的 config/.env。"""
    cfg._env = EnvView({}, {"REDIS_URL": url})
    return cfg


class TestRedisUrl:
    def test_missing_redis_url_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="REDIS_URL"):
            redis_url(make_config(tmp_path))

    def test_rejects_wrong_scheme(self, tmp_path):
        cfg = with_redis(make_config(tmp_path), "http://localhost:6379/0")
        with pytest.raises(ConfigError, match="redis://"):
            redis_url(cfg)

    @pytest.mark.parametrize("url", ["redis://localhost:6379/0",
                                     "rediss://example.test:6380/0"])
    def test_accepts_redis_schemes(self, tmp_path, url):
        assert redis_url(with_redis(make_config(tmp_path), url)) == url


class TestBuildCeleryApp:
    def test_broker_comes_from_redis_url(self, tmp_path):
        cfg = with_redis(make_config(tmp_path), "redis://127.0.0.1:6379/3")
        assert build_celery_app(cfg).conf.broker_url == "redis://127.0.0.1:6379/3"

    def test_limits_come_from_queue_section(self, tmp_path):
        body = "[queue]\ntask_soft_time_limit_s = 30\ntask_time_limit_s = 45\n"
        cfg = with_redis(make_config(tmp_path, body), "redis://127.0.0.1:6379/0")
        conf = build_celery_app(cfg).conf
        assert conf.task_soft_time_limit == 30
        assert conf.task_time_limit == 45

    def test_no_result_backend(self, tmp_path):
        """运行状态以数据库为准（ADR 0011）：不设 result backend。

        设了后端会让 send_task 顺带碰后端，broker 不可达时抛裸 RuntimeError 且要等好几秒
        （实测 6.8 秒），不设后端则直接抛 kombu.OperationalError，能精确映射 503。
        """
        cfg = with_redis(make_config(tmp_path), "redis://127.0.0.1:6379/0")
        assert not build_celery_app(cfg).conf.result_backend

    def test_broker_fails_fast(self, tmp_path):
        cfg = with_redis(make_config(tmp_path), "redis://127.0.0.1:6379/0")
        options = build_celery_app(cfg).conf.broker_transport_options
        assert options["socket_connect_timeout"] == SOCKET_TIMEOUT_S
        assert options["retry_policy"]["max_retries"] == RETRY_POLICY["max_retries"]

    def test_takes_over_celery_logging(self, tmp_path, monkeypatch):
        """S4.1：连上 `setup_logging` 信号等于声明"日志由我们配"（Celery 就不再套自己的 dictConfig）。"""
        from celery.signals import setup_logging

        from xhs_agent.tasks import celery_app as celery_app_module

        cfg = make_config(tmp_path, 'log_level = "DEBUG"\nlog_format = "json"\n')
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(celery_app_module, "configure_logging",
                            lambda level, log_format: seen.append((level, log_format)))

        handler = install_logging(cfg)
        try:
            setup_logging.send(sender=None)
        finally:
            setup_logging.disconnect(handler)
        assert seen == [("DEBUG", "json")]


class TestTaskRegistration:
    def test_registers_named_task_with_queue_retry_budget(self, tmp_path):
        cfg = with_redis(make_config(tmp_path, "[queue]\nmax_retries = 3\n"),
                         "redis://127.0.0.1:6379/0")
        app = build_celery_app(cfg)
        task = register_analyze_task(app, cfg)
        assert task.name == ANALYZE_TASK_NAME
        # Celery 的 @app.task 返回 PromiseProxy，不能拿 `is` 比；按注册名与属性断言
        registered = app.tasks[ANALYZE_TASK_NAME]
        assert registered.name == ANALYZE_TASK_NAME
        assert registered.max_retries == 3


class TestStaleRunReconcile:
    """S4.1 的对账接线：阈值来自 `[queue].task_time_limit_s × 2`，由 worker_ready 触发一次。"""

    def test_threshold_is_double_the_hard_limit(self, tmp_path):
        cfg = make_config(tmp_path, "[queue]\ntask_soft_time_limit_s = 30\n"
                                    "task_time_limit_s = 45\n")
        assert stale_threshold_s(cfg) == 90

    def test_worker_ready_reconcile_runs_once(self, tmp_path, monkeypatch):
        from celery.signals import worker_ready

        cfg = with_redis(make_config(tmp_path), "redis://127.0.0.1:6379/0")
        seen: list[AppConfig] = []
        monkeypatch.setattr(reconcile_task, "run_reconcile",
                            lambda cfg, **_kwargs: seen.append(cfg) or [])

        handler = install_worker_ready_reconcile(cfg)
        try:
            # Celery 真实发的是 sender=<Consumer>（不是 app）：按 app 过滤会让对账永不触发
            worker_ready.send(sender=object())
        finally:
            worker_ready.disconnect(handler)
        assert seen == [cfg]

    def test_reconcile_failed_logs_and_returns_empty(self, tmp_path, monkeypatch):
        """对账失败只记日志：绝不因为数据库抖动挡住 worker 启动。"""
        cfg = make_config(tmp_path)
        monkeypatch.setattr(reconcile_task, "_reconcile",
                            _boom(RuntimeError, "数据库炸了"))
        assert reconcile_task.run_reconcile(cfg) == []


class TestTransientClassification:
    def test_infrastructure_errors_are_retried(self):
        for exc_type in (OperationalError, InterfaceError, ConnectionError,
                         TimeoutError, OSError):
            assert exc_type in TRANSIENT_ERRORS

    def test_analysis_errors_are_not_retried(self):
        """模型/向量已在客户端内重试过；整批重跑只是重复烧钱。"""
        for exc_type in (LLMError, EmbeddingError, SchemaError, ValueError, RuntimeError):
            assert not issubclass(exc_type, TRANSIENT_ERRORS)


class FakeTask:
    """任务外壳用的假 Task：只提供 request.retries / max_retries / retry()。"""

    def __init__(self, *, retries: int = 0, max_retries: int = 2) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.max_retries = max_retries
        self.retry_calls: list[tuple[str, int]] = []

    def retry(self, exc: BaseException, countdown: int) -> BaseException:
        self.retry_calls.append((type(exc).__name__, countdown))
        return Retry(str(exc))


def _boom(exc_type: type[BaseException], message: str = "炸了"):
    async def _raise(*_args, **_kwargs):
        raise exc_type(message)
    return _raise


class TestExecuteSemantics:
    def test_transient_error_retries_with_backoff(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        task = FakeTask(retries=0, max_retries=2)
        monkeypatch.setattr(analysis, "run_job", _boom(ConnectionError))
        monkeypatch.setattr(analysis, "mark_failed",
                            lambda *_a, **_k: pytest.fail("还没到重试上限，不该写 failed"))

        with pytest.raises(Retry):
            execute(task, "job-1", cfg)
        assert task.retry_calls == [("ConnectionError", 2)]

    def test_exhausted_budget_marks_failed_and_reraises(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        task = FakeTask(retries=2, max_retries=2)
        marked: list[tuple[str, str]] = []
        monkeypatch.setattr(analysis, "run_job", _boom(ConnectionError))
        monkeypatch.setattr(analysis, "mark_failed",
                            lambda job_id, _cfg, detail: marked.append((job_id, detail)))

        with pytest.raises(ConnectionError):
            execute(task, "job-1", cfg)
        assert task.retry_calls == []
        assert marked and marked[0][0] == "job-1"
        assert "ConnectionError" in marked[0][1]

    def test_analysis_error_marks_failed_without_retry(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        task = FakeTask(retries=0, max_retries=2)
        marked: list[str] = []
        monkeypatch.setattr(analysis, "run_job", _boom(LLMError))
        monkeypatch.setattr(analysis, "mark_failed",
                            lambda _job_id, _cfg, detail: marked.append(detail))

        with pytest.raises(LLMError):
            execute(task, "job-1", cfg)
        assert task.retry_calls == []          # 不重试
        assert marked and "LLMError" in marked[0]

    def test_soft_time_limit_marks_failed_without_retry(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)          # [queue].task_soft_time_limit_s 默认 600
        task = FakeTask(retries=0, max_retries=2)
        marked: list[str] = []
        monkeypatch.setattr(analysis, "run_job", _boom(SoftTimeLimitExceeded))
        monkeypatch.setattr(analysis, "mark_failed",
                            lambda _job_id, _cfg, detail: marked.append(detail))

        with pytest.raises(SoftTimeLimitExceeded):
            execute(task, "job-1", cfg)
        assert task.retry_calls == []
        assert marked and "软超时" in marked[0]
