"""Analyze node: LLM structured summaries for each collected source."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from workflows.model_client import accumulate_usage, chat_json
from workflows.node_support import merge_analysis
from workflows.state import KBState

logger = logging.getLogger(__name__)


def analyze_node(state: KBState) -> dict[str, Any]:
    """Analyze collected sources with LLM into structured knowledge summaries."""

    logger.info("[AnalyzeNode] LLM 分析采集数据")

    analyses = []
    tracker = dict(state.get("cost_tracker") or {})
    system = "你是 AI 技术情报分析员。只输出严格 JSON，不要输出 Markdown。"

    for source in state.get("sources", []):
        prompt = (
            "请分析以下 GitHub 仓库，生成结构化中文知识摘要。\n"
            "输出 JSON 字段：title, source_url, summary, tags, language, "
            "quality_score, difficulty, metadata。\n"
            "quality_score 必须是 0 到 1 的小数，tags 为 3-5 个字符串。\n\n"
            f"输入数据：\n{json.dumps(source, ensure_ascii=False, indent=2)}"
        )
        parsed, usage = chat_json(prompt, system=system)
        tracker = accumulate_usage(tracker, usage)

        llm = parsed if isinstance(parsed, Mapping) else {}
        analyses.append(merge_analysis(source, llm))

    return {"analyses": analyses, "cost_tracker": tracker}
