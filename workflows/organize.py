"""Organize node: filter, dedupe, and optionally revise articles from review feedback."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from workflows.model_client import accumulate_usage, chat_json
from workflows.node_support import (
    analysis_to_article,
    article_with_defaults,
    dedupe_by_url,
    quality_score,
    state_int,
)
from workflows.planner import plan_value
from workflows.state import KBState

logger = logging.getLogger(__name__)


def organize_node(state: KBState) -> dict[str, Any]:
    """Filter, deduplicate, and optionally revise articles using review feedback."""

    logger.info("[OrganizeNode] 过滤、去重并整理知识条目")

    tracker = dict(state.get("cost_tracker") or {})
    candidates = [analysis_to_article(item) for item in state.get("analyses", [])]
    rel_raw = plan_value(state, "relevance_threshold", default=0.6)
    try:
        relevance_threshold = float(rel_raw)
    except (TypeError, ValueError):
        relevance_threshold = 0.6
    articles = dedupe_by_url(
        article for article in candidates if quality_score(article) >= relevance_threshold
    )

    feedback = str(state.get("review_feedback") or "").strip()
    iteration = state_int(state, "iteration", default=0)
    if iteration > 0 and feedback and articles:
        system = "你是知识库质量修订助手。只输出严格 JSON，不要输出 Markdown。"
        prompt = (
            "请根据审核反馈，对知识条目做定向修改。保持 URL、id 和事实来源不变，"
            "只优化 summary/tags/metadata/status 等结构化摘要字段。\n"
            '输出 JSON：{"articles": [...]}。\n\n'
            f"审核反馈：{feedback}\n\n"
            f"当前条目：\n{json.dumps(articles, ensure_ascii=False, indent=2)}"
        )
        parsed, usage = chat_json(prompt, system=system)
        tracker = accumulate_usage(tracker, usage)
        revised = parsed.get("articles") if isinstance(parsed, Mapping) else None
        if isinstance(revised, list):
            articles = dedupe_by_url(
                article_with_defaults(item)
                for item in revised
                if isinstance(item, Mapping) and quality_score(item) >= relevance_threshold
            )

    return {"articles": articles, "cost_tracker": tracker}
