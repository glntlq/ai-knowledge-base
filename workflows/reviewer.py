"""Reviewer node: audit `analyses` with weighted multi-dimension scores."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from workflows.model_client import accumulate_usage, chat_json
from workflows.node_constants import MAX_REVIEW_ITERATIONS
from workflows.node_support import state_int
from workflows.state import KBState

logger = logging.getLogger(__name__)

# 仅送审前 N 条，控制 token
REVIEW_ANALYSES_LIMIT = 5

# 各维度 1–10 分权重（总和为 1）
_DIM_WEIGHTS: dict[str, float] = {
    "summary_quality": 0.25,
    "technical_depth": 0.25,
    "relevance": 0.20,
    "originality": 0.15,
    "formatting": 0.15,
}

# 加权总分（1–10 标尺）达到该阈值则通过
_PASS_THRESHOLD = 7.0


def _clamp_1_10(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return max(1.0, min(10.0, x))


def _extract_dimension_scores(result: Mapping[str, Any]) -> dict[str, float] | None:
    raw_scores = result.get("scores")
    if not isinstance(raw_scores, Mapping):
        return None
    out: dict[str, float] = {}
    for key in _DIM_WEIGHTS:
        if key not in raw_scores:
            return None
        clamped = _clamp_1_10(raw_scores.get(key))
        if clamped is None:
            return None
        out[key] = clamped
    return out


def weighted_total_1_to_10(scores: Mapping[str, float]) -> float:
    """按固定权重重算加权总分（1–10 分标尺），不信任模型给出的总分。"""

    return float(sum(scores[k] * w for k, w in _DIM_WEIGHTS.items()))


def review_node(state: KBState) -> dict[str, Any]:
    """审核 `state['analyses']` 中前若干条，按加权总分决定是否通过。"""

    logger.info("[ReviewerNode] 审核 analyses 质量（最多 %d 条）", REVIEW_ANALYSES_LIMIT)

    iteration = state_int(state, "iteration", default=0)
    if iteration >= MAX_REVIEW_ITERATIONS - 1:
        return {
            "review_passed": True,
            "review_feedback": "已达到最大审核循环次数，强制通过并进入保存阶段。",
            "iteration": MAX_REVIEW_ITERATIONS,
            "cost_tracker": dict(state.get("cost_tracker") or {}),
        }

    tracker = dict(state.get("cost_tracker") or {})
    analyses = list(state.get("analyses") or [])[:REVIEW_ANALYSES_LIMIT]

    plan = str(state.get("plan") or "").strip()
    plan_block = f"\n计划说明（供参考）：\n{plan}\n" if plan else ""

    system = (
        "你是 AI 知识库质检员。只输出严格 JSON，不要输出 Markdown。"
        "不要自行计算总分或加权结果；各维度给出整数或一位小数的 1–10 分即可。"
    )
    prompt = (
        "请对以下「分析结果 analyses」从五个维度分别打分（每项 1–10 分，10 为最好）。\n"
        "维度与含义：\n"
        "- summary_quality：摘要是否准确、完整、可读\n"
        "- technical_depth：技术要点是否到位、有深度\n"
        "- relevance：与 AI/LLM/Agent 等主题的相关性\n"
        "- originality：是否有非常识性见解或清晰差异化信息\n"
        "- formatting：字段与标签等结构化信息是否规范、一致\n\n"
        "只输出 JSON，结构如下（不要包含 overall 或加权总分字段）：\n"
        '{"scores":{"summary_quality":0,"technical_depth":0,"relevance":0,'
        '"originality":0,"formatting":0},"feedback":"中文简要审稿意见"}\n'
        f"{plan_block}\n"
        f"待审核 analyses（共 {len(analyses)} 条）：\n"
        f"{json.dumps(analyses, ensure_ascii=False, indent=2)}"
    )

    try:
        parsed, usage = chat_json(prompt, system=system, temperature=0.1)
        tracker = accumulate_usage(tracker, usage)
    except Exception as exc:
        logger.warning("[ReviewerNode] LLM 调用失败，自动通过: %s", exc)
        return {
            "review_passed": True,
            "review_feedback": "LLM 审核调用失败，已自动通过，不阻塞流程。",
            "iteration": min(iteration + 1, MAX_REVIEW_ITERATIONS),
            "cost_tracker": tracker,
        }

    result = parsed if isinstance(parsed, Mapping) else {}
    dim_scores = _extract_dimension_scores(result)
    feedback = str(result.get("feedback") or "").strip()

    if dim_scores is None:
        logger.warning("[ReviewerNode] 模型输出缺少有效维度分数，自动通过")
        return {
            "review_passed": True,
            "review_feedback": (
                (feedback + "\n" if feedback else "")
                + "审核输出缺少完整维度分数，已自动通过，不阻塞流程。"
            ).strip(),
            "iteration": min(iteration + 1, MAX_REVIEW_ITERATIONS),
            "cost_tracker": tracker,
        }

    weighted = weighted_total_1_to_10(dim_scores)
    passed = weighted >= _PASS_THRESHOLD

    detail = (
        f"系统重算加权总分 {weighted:.2f}/10（阈值 {_PASS_THRESHOLD}）。"
        f"各维分：{json.dumps(dim_scores, ensure_ascii=False)}"
    )
    merged_feedback = "\n".join(part for part in (feedback, detail) if part).strip()

    return {
        "review_passed": passed,
        "review_feedback": merged_feedback,
        "iteration": min(iteration + 1, MAX_REVIEW_ITERATIONS),
        "cost_tracker": tracker,
    }
