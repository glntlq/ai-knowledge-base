"""LangGraph 知识库工作流共享状态定义。

状态字段遵循“报告式通信”原则：节点之间传递结构化摘要和必要指标，
不在状态中堆叠完整原始页面、长文本正文或未清洗的中间大对象。
"""

from typing import Any, NotRequired, TypedDict


class KBState(TypedDict):
    """知识库 LangGraph 工作流的共享状态。"""

    sources: list[dict[str, Any]]
    # 采集阶段输出的原始数据摘要列表；每项包含 source/source_url/title/description/metadata 等结构化字段，
    # 不直接保存完整 HTML、RSS XML 或超长正文。

    analyses: list[dict[str, Any]]
    # LLM 分析后的结构化结果列表；每项包含 summary/tags/language/difficulty/quality_score 等模型产物摘要，
    # 用于后续整理与审核，不保存完整 Prompt 或原始响应全文。

    articles: list[dict[str, Any]]
    # 格式化、去重后的知识条目列表；每项应接近 knowledge/articles/*.json 的最终 schema，
    # 包含 id/title/source_url/summary/tags/status/metadata 等字段。

    review_feedback: str
    # 审核节点给出的反馈意见摘要；记录需要修改的关键问题、风险或改进建议，
    # 不记录完整审稿对话。

    review_passed: bool
    # 审核是否通过；True 表示可进入保存或分发阶段，False 表示需要按反馈重新分析或整理。

    iteration: int
    # 当前审核循环次数；从 0 或 1 开始计数，工作流中最多允许 3 次，避免无限重试。

    cost_tracker: dict[str, Any]
    # Token 与成本追踪摘要；建议包含 prompt_tokens/completion_tokens/total_tokens/cost/provider/model 等字段，
    # 用于报告成本，不保存单次请求的完整上下文。

    plan: NotRequired[dict[str, Any]]
    # 可选：Planner 输出的策略 dict（tier、per_source_limit、relevance_threshold、max_iterations、rationale 等），只读。

    target_count: NotRequired[int]
    # 可选：覆盖 Planner 所用的目标采集量；缺省时由 PLANNER_TARGET_COUNT 环境变量决定。

    max_iterations: NotRequired[int]
    # 可选：与审核循环配套的上限；缺省时各节点使用 node_constants.MAX_REVIEW_ITERATIONS。

    needs_human_review: NotRequired[bool]
    # 可选：已写入 knowledge/pending_review/ 且流程应避开主库写入时为 True。
