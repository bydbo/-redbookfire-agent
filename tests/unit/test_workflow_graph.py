"""五节点图的离线单测：注入假模型 / 假检索 / 临时 runs 目录，不连库（S3.1）。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from langgraph.types import RetryPolicy

from xhs_agent.config import load_config
from xhs_agent.core.tracing import Generation, NullTracer, TracingProvider
from xhs_agent.schemas import Coverage, Element, MatchCandidate, Material, SchemaError
from xhs_agent.services.retrieval import RetrievalOutcome, VectorCoverage
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMError, LLMResult, StructuredCaller
from xhs_agent.tools.prompt import PromptError
from xhs_agent.tools.trace import RunStore
from xhs_agent.workflows import NODE_STEPS, run_analysis
from xhs_agent.workflows import analysis as workflow

MATERIAL_ID = "m_test_1"

CLUE_PAYLOAD = {
    "hotspot_raw": "模型改写版",
    "why_it_works": ["反差：明星身份 vs 业余球场"],
    "mechanisms": [{"name": "反差", "explain": "身份反差"}],
    "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.9, "confidence": 0.9,
                  "evidence": "热点原文片段"}],
    "match_keywords": ["羽毛球"],
    "borrow_angles": ["同款球场热场"],
}
EXPLAIN_PAYLOAD = {"candidates": [
    {"material_id": MATERIAL_ID, "rank": 1, "reasons": ["模型理由"], "usage": "模型用法"},
]}
DRAFT_PAYLOAD = {"titles": [{"text": "球场热身也能出片", "style": "直给"}], "body": "正文……",
                 "tags": ["羽毛球"], "cover_text": "封面", "first_3s": "开头",
                 "shot_list": ["先拍球场"], "compliance_notes": ["别用明星肖像"]}

FAST_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.01, backoff_factor=1.0,
                         max_interval=0.05, jitter=False, retry_on=workflow.retry_on_error)


class FakeProvider(llm.BaseProvider):
    """按任务返回预置 JSON；`fail_times` 前若干次返回上游错误（用于验证节点级重试）。"""

    name = "fake"

    def __init__(self, *, fail_times: int = 0) -> None:
        super().__init__(model="fake-1")
        self.fail_times = fail_times
        self.tasks: list[str] = []

    async def complete(self, call):
        self.tasks.append(call.task)
        if self.fail_times > 0:
            self.fail_times -= 1
            return LLMResult(text="", provider=self.name, model=self.model, error="上游 503")
        payload = {"hotspot_clue": CLUE_PAYLOAD, "material_select": EXPLAIN_PAYLOAD,
                   "copy_draft": DRAFT_PAYLOAD}[call.task]
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50,
                         cost_cny=0.0001)


class FakeTracer:
    """记录 run / hotspot / generation 三类事件的假追踪器（S4.2，离线用）。"""

    enabled = True

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.finished: dict = {}
        self.flushes = 0

    @contextmanager
    def run(self, *, name, metadata):
        self.events.append(("run", {"name": name, **metadata}))
        yield None

    @contextmanager
    def hotspot(self, *, index, metadata, content=""):
        self.events.append(("hotspot", {"index": index, **metadata}))
        yield None

    @contextmanager
    def generation(self, *, name, model, metadata):
        fields = Generation(metadata=dict(metadata))
        self.events.append(("generation", {"name": name, "model": model, **metadata}))
        yield fields
        event = self.events[-1][1]
        event["usage"] = dict(fields.usage_details)
        event["cost"] = dict(fields.cost_details)
        event["level"] = fields.level

    def finish_run(self, *, status, totals, run_id="", prompt_versions=None, error=""):
        self.finished = {"status": status, "totals": dict(totals), "run_id": run_id,
                         "prompt_versions": dict(prompt_versions or {}), "error": error}

    def flush(self):
        self.flushes += 1


def make_outcome(*, with_candidate: bool = True) -> RetrievalOutcome:
    """构造成与 `services.retrieval.retrieve_candidates` 同形的结果。"""
    vector = VectorCoverage(enabled=True, model="text-embedding-v3", total=19,
                            with_embedding=19, ratio=1.0)
    if not with_candidate:
        return RetrievalOutcome(
            candidates=[],
            coverage=Coverage(ratio=0.0, covered=[],
                              gaps=[Element(type="topic", value="羽毛球")]),
            vector_coverage=vector, literal_recalled=0, vector_recalled=0,
            candidates_considered=0)
    material = Material(id=MATERIAL_ID, path="D:/materials/球场热身.mp4", title="球场热身",
                        tags=["羽毛球"], width=1080, height=1920, duration_s=15)
    candidate = MatchCandidate(material_id=MATERIAL_ID, material=material, score=0.8123,
                               rank=1, recall_sources=["literal"],
                               reasons=["规则理由"], usage="规则用法")
    return RetrievalOutcome(
        candidates=[candidate],
        coverage=Coverage(ratio=1.0, covered=[Element(type="topic", value="羽毛球")], gaps=[]),
        vector_coverage=vector, literal_recalled=1, vector_recalled=0,
        candidates_considered=1)


def fake_retrieve(outcome: RetrievalOutcome):
    """记下每次调用的线索与 topk 覆盖，固定返回预置结果（签名对齐 retrieve_candidates）。"""
    calls: list[str] = []
    topks: list[int | None] = []

    async def _retrieve(session, cfg, clue, *, embedder=None, now=None, topk=None, http=None):
        calls.append(clue.hotspot_raw)
        topks.append(topk)
        return outcome

    _retrieve.calls = calls  # type: ignore[attr-defined]
    _retrieve.topks = topks  # type: ignore[attr-defined]
    return _retrieve


@pytest.fixture
def cfg(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{(tmp_path / "materials").as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


class TestRetryJudgement:
    """节点级重试：上游抖动要重试，契约/编程错误不重试。"""

    def test_transient_errors_are_retried(self):
        assert workflow.retry_on_error(LLMError("上游 503")) is True
        assert workflow.retry_on_error(ConnectionError("connection reset")) is True
        assert workflow.retry_on_error(TimeoutError("timeout")) is True

    def test_contract_errors_are_not_retried(self):
        assert workflow.retry_on_error(SchemaError("缺 elements")) is False
        assert workflow.retry_on_error(PromptError("缺变量")) is False
        assert workflow.retry_on_error(TypeError("坏类型")) is False

    def test_default_policy_uses_our_judgement(self):
        assert workflow.DEFAULT_RETRY.retry_on is workflow.retry_on_error
        assert workflow.DEFAULT_RETRY.max_attempts == 3


class TestRunAnalysis:
    @pytest.mark.asyncio
    async def test_reports_trace_spans_and_generations(self, cfg, monkeypatch):
        """S4.2：一次运行 = 1 个 run + N 个 hotspot + 每个热点 3 个 generation。"""
        monkeypatch.setattr(workflow, "retrieve_candidates", fake_retrieve(make_outcome()))
        tracer = FakeTracer()
        caller = StructuredCaller(provider=TracingProvider(
            FakeProvider(), tracer, price_in_per_m=2.16, price_out_per_m=8.64))

        result = await run_analysis(["热点一", "热点二"], cfg=cfg, session=None, caller=caller,
                                    tracer=tracer, trace_metadata={"job_id": "job-1"})

        kinds = [kind for kind, _payload in tracer.events]
        assert kinds == ["run", "hotspot", "generation", "generation", "generation",
                         "hotspot", "generation", "generation", "generation"]
        run_event = tracer.events[0][1]
        assert run_event["name"] == "analyze" and run_event["hotspots"] == 2
        assert run_event["job_id"] == "job-1"
        first_generation = tracer.events[2][1]
        assert first_generation["name"] == "hotspot_clue"
        assert first_generation["usage"] == {"input": 100, "output": 50, "total": 150}
        assert first_generation["cost"]["total"] == pytest.approx(100 / 1e6 * 2.16
                                                                 + 50 / 1e6 * 8.64)
        assert tracer.finished["status"] == "succeeded"
        assert tracer.finished["totals"]["llm_calls"] == 6
        assert tracer.finished["run_id"] == result.run_id
        assert tracer.finished["prompt_versions"] == {"hotspot_clue": 1, "material_select": 1,
                                                     "copy_draft": 1}
        assert tracer.flushes == 1

    @pytest.mark.asyncio
    async def test_tracing_disabled_keeps_pipeline_working(self, cfg, monkeypatch):
        """缺键（NullTracer）时整批照常跑通——追踪是旁路组件。"""
        monkeypatch.setattr(workflow, "retrieve_candidates", fake_retrieve(make_outcome()))
        monkeypatch.setattr(workflow, "get_tracer", lambda _cfg: NullTracer())
        result = await run_analysis(["某明星打羽毛球"], cfg=cfg, session=None,
                                    caller=StructuredCaller(provider=FakeProvider()))
        assert result.status == "succeeded"
        assert result.prompt_versions == {"hotspot_clue": 1, "material_select": 1,
                                          "copy_draft": 1}

    @pytest.mark.asyncio
    async def test_walks_five_nodes_and_collects_prompt_versions(self, cfg, monkeypatch):
        retrieve = fake_retrieve(make_outcome())
        monkeypatch.setattr(workflow, "retrieve_candidates", retrieve)
        provider = FakeProvider()

        result = await run_analysis(["某明星打羽毛球"], cfg=cfg, session=None,
                                    caller=StructuredCaller(provider=provider))

        assert result.status == "succeeded"
        assert result.prompt_versions == {"hotspot_clue": 1, "material_select": 1,
                                          "copy_draft": 1}
        assert retrieve.calls == ["某明星打羽毛球"]
        assert retrieve.topks == [None]        # 没传 run_id / topk 时不做覆盖
        assert len(result.hotspots) == 1

        entry = result.hotspots[0]
        assert entry["clue"]["hotspot_raw"] == "某明星打羽毛球"
        assert entry["candidates"][0]["reasons"] == ["模型理由"]     # 模型覆盖规则解释
        assert entry["candidates"][0]["usage"] == "模型用法"
        assert entry["draft"]["body"] == "正文……"
        assert entry["coverage"]["ratio"] == 1.0

        # ADR 0011：运行状态以数据库为准，产物目录只留 trace.jsonl 与报告（不再有 state.json）
        run_dir = Path(result.report_paths["markdown"]).parent
        assert not (run_dir / "state.json").exists()
        events = [json.loads(line) for line
                  in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        steps = [event["payload"]["name"] for event in events if event["event"] == "step_started"]
        assert steps == list(NODE_STEPS)
        assert events[-1]["event"] == "run_finished"
        assert events[-1]["payload"]["status"] == "succeeded"
        assert events[-1]["payload"]["totals"]["llm_calls"] == 3
        assert events[-1]["payload"]["totals"]["cost_cny"] == pytest.approx(0.0003)

    @pytest.mark.asyncio
    async def test_report_contains_hotspot_and_vector_coverage(self, cfg, monkeypatch):
        monkeypatch.setattr(workflow, "retrieve_candidates", fake_retrieve(make_outcome()))
        result = await run_analysis(["某明星打羽毛球"], cfg=cfg, session=None,
                                    caller=StructuredCaller(provider=FakeProvider()))
        markdown = Path(result.report_paths["markdown"]).read_text(encoding="utf-8")
        html = Path(result.report_paths["html"]).read_text(encoding="utf-8")
        assert "某明星打羽毛球" in markdown and "球场热身" in markdown
        assert "向量覆盖率 19/19（100%）" in markdown
        assert "向量覆盖率 19/19（100%）" in html
        assert "<html" in html

    @pytest.mark.asyncio
    async def test_multiple_hotspots_share_one_report(self, cfg, monkeypatch):
        retrieve = fake_retrieve(make_outcome())
        monkeypatch.setattr(workflow, "retrieve_candidates", retrieve)
        result = await run_analysis(["热点一", "热点二"], cfg=cfg, session=None,
                                    caller=StructuredCaller(provider=FakeProvider()))
        assert retrieve.calls == ["热点一", "热点二"]
        assert len(result.hotspots) == 2
        markdown = Path(result.report_paths["markdown"]).read_text(encoding="utf-8")
        assert "热点一" in markdown and "热点二" in markdown
        assert result.prompt_versions == {"hotspot_clue": 1, "material_select": 1,
                                          "copy_draft": 1}

    @pytest.mark.asyncio
    async def test_without_candidates_skips_explain_and_draft(self, cfg, monkeypatch):
        monkeypatch.setattr(workflow, "retrieve_candidates",
                            fake_retrieve(make_outcome(with_candidate=False)))
        provider = FakeProvider()
        result = await run_analysis(["冷门热点"], cfg=cfg, session=None,
                                    caller=StructuredCaller(provider=provider))
        assert result.status == "succeeded"
        assert result.prompt_versions == {"hotspot_clue": 1}   # 未调用就不出现
        assert provider.tasks == ["hotspot_clue"]
        entry = result.hotspots[0]
        assert entry["draft"] is None
        assert entry["candidates"] == []
        assert entry["gap_advice"][0]["type"] == "topic"
        assert "羽毛球" in Path(result.report_paths["markdown"]).read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_node_retry_recovers_from_transient_failure(self, cfg, monkeypatch):
        monkeypatch.setattr(workflow, "retrieve_candidates", fake_retrieve(make_outcome()))
        provider = FakeProvider(fail_times=2)   # 拆解节点前两次失败、第三次成功
        result = await run_analysis(["某明星打羽毛球"], cfg=cfg, session=None,
                                    caller=StructuredCaller(provider=provider),
                                    retry_policy=FAST_RETRY)
        assert result.status == "succeeded"
        assert provider.tasks.count("hotspot_clue") == 3
        assert len(result.hotspots) == 1

    @pytest.mark.asyncio
    async def test_failed_hotspot_is_recorded_and_batch_continues(self, cfg, monkeypatch):
        monkeypatch.setattr(workflow, "retrieve_candidates", fake_retrieve(make_outcome()))
        result = await run_analysis(["热点一", "热点二"], cfg=cfg, session=None,
                                    caller=StructuredCaller(provider=FakeProvider(fail_times=99)),
                                    retry_policy=FAST_RETRY)
        assert result.status == "failed"
        assert len(result.errors) == 2
        assert len(result.hotspots) == 2
        assert all(entry["error"] for entry in result.hotspots)
        markdown = Path(result.report_paths["markdown"]).read_text(encoding="utf-8")
        assert "本次运行" in markdown

    @pytest.mark.asyncio
    async def test_empty_hotspots_are_rejected(self, cfg):
        with pytest.raises(ValueError):
            await run_analysis([], cfg=cfg, session=None,
                               caller=StructuredCaller(provider=FakeProvider()))
        with pytest.raises(ValueError):
            await run_analysis(["   "], cfg=cfg, session=None,
                               caller=StructuredCaller(provider=FakeProvider()))

    @pytest.mark.asyncio
    async def test_injected_store_is_reused(self, cfg, monkeypatch, tmp_path):
        monkeypatch.setattr(workflow, "retrieve_candidates", fake_retrieve(make_outcome()))
        store = RunStore(str(tmp_path / "custom-runs"), "自定义")
        result = await run_analysis(["热点"], cfg=cfg, session=None, store=store,
                                    caller=StructuredCaller(provider=FakeProvider()))
        assert result.run_id == store.run_id
        assert Path(result.report_paths["markdown"]).parent == Path(store.dir)

    @pytest.mark.asyncio
    async def test_explicit_topk_is_forwarded_to_retrieval(self, cfg, monkeypatch):
        """显式 topk 会透传到检索层（收口 runs.topk：提交时传的上限必须真的生效）。"""
        retrieve = fake_retrieve(make_outcome())
        monkeypatch.setattr(workflow, "retrieve_candidates", retrieve)
        await run_analysis(["热点"], cfg=cfg, session=None, topk=3,
                           caller=StructuredCaller(provider=FakeProvider()))
        assert retrieve.topks == [3]

    @pytest.mark.asyncio
    async def test_preset_clue_skips_extraction(self, cfg, tmp_path):
        """线索复用：state 预置 clue 时拆解节点不调模型，但步骤照记（数据契约 §3.2）。"""
        store = RunStore(str(tmp_path / "runs"), "复用")
        provider = FakeProvider()
        fake = fake_retrieve(make_outcome())

        async def retrieve(clue):
            return await fake(None, None, clue)   # build_graph 的 retrieve 只吃线索

        graph = workflow.build_graph(caller=StructuredCaller(provider=provider),
                                     retrieve=retrieve, store=store)
        state = await graph.ainvoke({"hotspot_raw": "某明星打羽毛球", "clue": CLUE_PAYLOAD})
        assert "hotspot_clue" not in provider.tasks
        assert provider.tasks == ["material_select", "copy_draft"]
        assert state["clue"] == CLUE_PAYLOAD
        steps = store.state["steps"]
        assert [step["name"] for step in steps] == list(NODE_STEPS)
        assert steps[0]["detail"] == "复用已有线索"
