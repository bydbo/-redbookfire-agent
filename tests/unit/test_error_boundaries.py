"""公开错误边界：上游响应、请求与模型原文不能通过异常进入 API / 对话 / 任务。"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from fastapi import FastAPI
from kombu.exceptions import KombuError
from redis.exceptions import RedisError
from sqlalchemy.exc import InterfaceError, OperationalError

from xhs_agent.api.deps import CeleryDispatcher
from xhs_agent.api.errors import register_exception_handlers
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.budget import BudgetError
from xhs_agent.core.errors import (
    BadRequestError,
    DependencyUnavailableError,
    public_error_message,
)
from xhs_agent.schemas import SchemaError
from xhs_agent.services.chat_tools import _reason
from xhs_agent.tasks import analysis
from xhs_agent.tools.embedding import EmbeddingError
from xhs_agent.tools.llm import LLMError
from xhs_agent.tools.prompt import PromptError

PRIVATE = "PRIVATE_MODEL_REQUEST_SENTINEL"


@pytest.mark.parametrize("error_type", [
    ValueError, RuntimeError, LLMError, EmbeddingError, SchemaError, PromptError, TimeoutError,
])
def test_tool_errors_only_expose_safe_explanation(error_type):
    error = error_type(PRIVATE)
    assert PRIVATE not in public_error_message(error)
    assert PRIVATE not in _reason(error)
    assert error_type.__name__ in public_error_message(error)


def test_actionable_business_error_is_preserved():
    message = "指定的基础版本不属于当前会话，请重新选择"
    assert public_error_message(BadRequestError(message)) == message
    assert _reason(BadRequestError(message)) == message


@pytest.mark.parametrize("reason", ["required_context_overflow", "history_group_broken"])
def test_budget_explanation_does_not_echo_invalid_history(reason):
    message = public_error_message(BudgetError(PRIVATE, reason=reason))
    assert PRIVATE not in message
    assert "重试" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "status"), [
    (LLMError(PRIVATE), 502),
    (EmbeddingError(PRIVATE), 502),
    (RedisError(PRIVATE), 503),
    (KombuError(PRIVATE), 503),
    (OperationalError("SELECT " + PRIVATE, {}, RuntimeError(PRIVATE)), 503),
    (InterfaceError("SELECT " + PRIVATE, {}, RuntimeError(PRIVATE)), 503),
])
async def test_api_error_details_do_not_echo_dependency_exception(error, status):
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/probe")
    async def probe():
        raise error

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/probe")

    assert response.status_code == status
    assert PRIVATE not in response.text
    assert response.json()["detail"]["error"] == type(error).__name__


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enqueue", "enqueue_index", "task_state"])
async def test_queue_wrapper_does_not_copy_exception_into_public_detail(operation, monkeypatch):
    dispatcher = CeleryDispatcher(AppConfig())

    def fail(*_args, **_kwargs):
        raise KombuError(PRIVATE)

    dispatcher._app = SimpleNamespace(send_task=fail)
    monkeypatch.setattr(dispatcher, "_read_state", fail)
    with pytest.raises(DependencyUnavailableError) as caught:
        if operation == "enqueue_index":
            await dispatcher.enqueue_index()
        else:
            await getattr(dispatcher, operation)("job-test")

    assert PRIVATE not in str(caught.value.detail)
    assert caught.value.detail["error"] == "KombuError"


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError, SoftTimeLimitExceeded])
def test_worker_failure_persists_safe_error(error_type, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    cfg = load_config(str(config))
    error = error_type(PRIVATE)
    saved = []

    async def fail_job(*_args, **_kwargs):
        raise error

    def save_failure(job_id, _cfg, message, **_kwargs):
        saved.append((job_id, message))

    monkeypatch.setattr(analysis, "run_job", fail_job)
    monkeypatch.setattr(analysis, "mark_failed", save_failure)
    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0), max_retries=0)
    with pytest.raises(error_type):
        analysis.execute(task, "job-test", cfg)

    assert len(saved) == 1
    assert saved[0][0] == "job-test"
    assert PRIVATE not in saved[0][1]
