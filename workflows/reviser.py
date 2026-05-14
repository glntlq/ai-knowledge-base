"""Reviser node: rewrite analyses using review feedback."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from workflows.model_client import accumulate_usage, chat_json
from workflows.state import KBState

logger = logging.getLogger(__name__)


def revise_node(state: KBState) -> dict[str, Any]:
    """根据 `review_feedback` 调用 LLM 修订 `analyses`。

    当 `analyses` 或 `review_feedback`（去空白后）为空时跳过，返回 ``{}``。
    否则返回 ``{"analyses": improved, "cost_tracker": tracker}``。
    """

    analyses = state.get("analyses") or []
    feedback = str(state.get("review_feedback") or "").strip()

    if not analyses or not feedback:
        logger.info("[ReviserNode] analyses 或 review_feedback 为空，跳过修订")
        return {}

    tracker = dict(state.get("cost_tracker") or {})

    system = "你是 AI 知识库分析修订助手。只输出严格 JSON，不要输出 Markdown。"
    prompt = (
        "请根据以下「审核反馈」对「当前 analyses」做定向修订：可改写 summary、tags、"
        "language、difficulty、quality_score、metadata 等模型产物字段，使内容更符合反馈要求；"
        "保持 source_url、author、collected_at、published_at 等与事实来源绑定的字段不变，"
        "不要编造不存在的仓库或链接。\n\n"
        f"审核反馈：\n{feedback}\n\n"
        "当前 analyses：\n"
        f"{json.dumps(analyses, ensure_ascii=False, indent=2)}\n\n"
        '只输出 JSON，结构为：{"analyses": [<与输入条数一致、顺序对应的对象数组>]}。'
    )

    try:
        parsed, usage = chat_json(prompt, system=system, temperature=0.4)
        tracker = accumulate_usage(tracker, usage)
    except Exception as exc:
        logger.warning("[ReviserNode] LLM 调用失败，不更新 analyses: %s", exc)
        return {}

    raw = parsed.get("analyses") if isinstance(parsed, Mapping) else None
    if not isinstance(raw, list) or len(raw) != len(analyses):
        logger.warning(
            "[ReviserNode] 模型返回的 analyses 非法或与输入条数不一致（期望 %d 条），忽略更新",
            len(analyses),
        )
        return {}

    improved: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            logger.warning("[ReviserNode] analyses[%d] 非 JSON 对象，忽略更新", i)
            return {}
        improved.append(dict(item))

    logger.info("[ReviserNode] 已根据审核反馈修订 %d 条 analyses", len(improved))
    return {"analyses": improved, "cost_tracker": tracker}
