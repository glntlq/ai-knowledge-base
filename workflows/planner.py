"""Planner: choose workflow parameters from target scale (plan only, no side effects)."""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping

from workflows.state import KBState

logger = logging.getLogger(__name__)

_ENV_TARGET = "PLANNER_TARGET_COUNT"


def plan_strategy(target_count: int | None = None) -> dict[str, Any]:
    """根据目标采集规模返回策略 dict（只规划，不执行 I/O）。

    分档规则：

    - **lite**（target < 10）：单源条数少、相关性阈值高、审核迭代少（偏保守控本）。
    - **standard**（10 <= target < 20）：折中。
    - **full**（target >= 20）：单源上限高、阈值略低、迭代多（数据多时仍控成本但给足质量空间）。

    Args:
        target_count: 目标条数；为 ``None`` 时从环境变量 ``PLANNER_TARGET_COUNT`` 读取，默认 ``10``。

    Returns:
        包含 ``tier``、``target_count``、``per_source_limit``、``relevance_threshold``、
        ``max_iterations``、``rationale`` 等字段的 dict。
    """

    if target_count is None:
        raw = os.getenv(_ENV_TARGET) or "10"
        try:
            target_count = int(raw)
        except ValueError:
            target_count = 10

    if target_count < 10:
        return {
            "tier": "lite",
            "target_count": target_count,
            "per_source_limit": 5,
            "relevance_threshold": 0.7,
            "max_iterations": 1,
            "rationale": (
                "目标采集量小于 10：整体数据量小，采用 lite——限制单源条数、提高相关性阈值、"
                "将审核迭代降为 1，优先控 token 与成本，避免在小规模任务上过度消耗。"
            ),
        }
    if target_count < 20:
        return {
            "tier": "standard",
            "target_count": target_count,
            "per_source_limit": 10,
            "relevance_threshold": 0.5,
            "max_iterations": 2,
            "rationale": (
                "目标采集量在 10–19：采用 standard——在单源上限、相关性门槛与审核轮数之间折中，"
                "兼顾质量与成本，适合日常批处理。"
            ),
        }
    return {
        "tier": "full",
        "target_count": target_count,
        "per_source_limit": 20,
        "relevance_threshold": 0.4,
        "max_iterations": 3,
        "rationale": (
            "目标采集量不少于 20：数据规模大，采用 full——允许更高单源上限、略放宽相关性阈值、"
            "审核迭代为 3，在可控成本下给模型更多纠错与打磨空间。"
        ),
    }


def plan_value(state: Mapping[str, Any], key: str, default: Any) -> Any:
    """从 ``state['plan']`` 读取键；无 plan 或缺键时返回 ``default``。"""

    raw = state.get("plan")
    if not isinstance(raw, Mapping):
        return default
    val = raw.get(key)
    return default if val is None else val


def planner_node(state: KBState) -> dict[str, Any]:
    """LangGraph 入口节点：生成 ``plan`` 写入状态，供下游只读使用。"""

    raw = state.get("target_count")
    tc: int | None
    if raw is None:
        tc = None
    else:
        try:
            tc = int(raw)
        except (TypeError, ValueError):
            tc = None

    plan = plan_strategy(target_count=tc)
    logger.info(
        "[PlannerNode] tier=%s target_count=%s per_source_limit=%s max_iterations=%s",
        plan["tier"],
        plan["target_count"],
        plan["per_source_limit"],
        plan["max_iterations"],
    )
    return {"plan": plan}
