"""轮次上下文策略（v2 §3.6 / E7CTX-15）：一次解析、处处使用。

`TurnContextPolicy` 在轮次入口由 `[context]` 配置**一次**解析出来，随后传给状态、版本、
预算、缓存与工具；任何模块都不得只看 `image_cache_enabled` 之类单个开关自行决定是否启用
新业务。四种规定行为：

======  ==============================================================
模式    行为
======  ==============================================================
off     不新增抽取、版本提交、持久缓存复用或新历史装配；保留读取历史能力
shadow  候选状态和预算只用于比较；不改正式 prompt、对象、版本或工具结果；
        候选调用仍计费
enforce 完整状态、版本、预算和缓存规则（`image_cache_enabled` 是其细分开关）
======  ==============================================================

模型窗口未核实时 enforce 降级为 shadow，`mode_reason=window_unverified`；
`configured_off` / `session_not_enabled` 同理。回退（mode 调回 off）保留状态、审计与版本；
已有未解决冲突的受影响任务不允许静默走旧逻辑（由轮次入口的状态检查兜住）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig

MODES = ("off", "shadow", "enforce")


@dataclass(frozen=True)
class TurnContextPolicy:
    """一次轮次的上下文策略（不可变；由入口构建后传递）。"""

    session_id: str = ""
    mode: str = "off"                       # 运行时实际生效模式
    mode_reason: str = ""                   # configured_off / session_not_enabled / window_unverified
    application_limit: int = 65536
    model_window: int | None = None
    output_tokens: int = 16000
    image_cache_enabled: bool = True
    extract_max_tokens: int = 2048
    prefix_usage_supported: bool = False
    stream_usage_supported: bool = False
    thinking_toggle_supported: bool = False

    @classmethod
    def from_config(cls, cfg: AppConfig, session_id: str) -> TurnContextPolicy:
        """入口唯一构建点：`[context]` + `[llm]` / `[vision]` 能力开关一次解析。"""
        context = cfg.context
        limit, _estimator = context.effective_limit()
        return cls(
            session_id=str(session_id),
            mode=context.effective_mode(session_id),
            mode_reason=context.mode_reason(session_id),
            application_limit=limit,
            model_window=context.model_context_window,
            output_tokens=int(cfg.llm.max_tokens),
            image_cache_enabled=bool(context.image_cache_enabled),
            extract_max_tokens=int(context.extract_max_tokens),
            prefix_usage_supported=bool(cfg.llm.prefix_usage_supported),
            stream_usage_supported=bool(cfg.llm.stream_usage_supported),
            thinking_toggle_supported=bool(cfg.llm.thinking_toggle_supported),
        )

    # ---- 规定行为（后续模块只问策略，不自行读配置）----

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    @property
    def shadow(self) -> bool:
        return self.mode == "shadow"

    @property
    def checks_budget(self) -> bool:
        """shadow / enforce 都做预算检查（off 不做，保持旧行为）。"""
        return self.mode in ("shadow", "enforce")

    @property
    def extracts_state(self) -> bool:
        """状态抽取：shadow / enforce 都跑（候选调用照常计费）；off 不跑。"""
        return self.mode in ("shadow", "enforce")

    @property
    def grouped_history(self) -> bool:
        """完整轮次组装配只属于 enforce；shadow 不改正式 prompt（E7CTX-15）。"""
        return self.enforce

    @property
    def commits_versions(self) -> bool:
        """版本登记与提交只在 enforce；shadow 不登记业务版本（§2.7）。"""
        return self.enforce

    @property
    def persistent_cache(self) -> bool:
        """持久缓存复用只在 enforce（且细分开关打开）；off / shadow 都不走。"""
        return self.enforce and self.image_cache_enabled

    @property
    def blocks_on_unresolved(self) -> bool:
        """未解决冲突 / 未完成恢复阻止依赖任务：enforce 生效；shadow 只记录。"""
        return self.enforce
