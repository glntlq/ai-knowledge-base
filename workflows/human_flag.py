"""HumanFlag node: offload stuck workflow payloads for manual review."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from workflows.node_constants import MAX_REVIEW_ITERATIONS, ROOT_DIR
from workflows.node_support import state_int
from workflows.planner import plan_value
from workflows.state import KBState

logger = logging.getLogger(__name__)

# 与主库 `knowledge/articles/` 隔离，仅存放待人工复核的快照
PENDING_REVIEW_DIR = ROOT_DIR / "knowledge" / "pending_review"


def human_flag_node(state: KBState) -> dict[str, Any]:
    """当审核在迭代上限内仍未通过时，将当前条目快照落盘并给出路状态。

    触发条件（同时满足）：

    - ``review_passed`` 为假；
    - ``iteration >= max_iterations``，其中 ``max_iterations`` 取自
      ``state["max_iterations"]``，缺省为 Planner 的 ``plan["max_iterations"]``，
      再缺省为 ``MAX_REVIEW_ITERATIONS``。

    未触发时返回 ``{}``。触发时在 ``knowledge/pending_review/`` 下写入 JSON，
    并返回 ``needs_human_review``、清空 ``articles``（避免后续误入主库保存），
    以及将 ``review_passed`` 置为真以便图中可作为循环出口（请优先接到 END
    或配合 ``save_node`` 对 ``needs_human_review`` 的跳过逻辑）。
    """

    if bool(state.get("review_passed")):
        logger.info("[HumanFlagNode] 审核已通过，无需人工标记")
        return {}

    raw_mx = plan_value(state, "max_iterations", default=MAX_REVIEW_ITERATIONS)
    try:
        default_max = int(raw_mx)
    except (TypeError, ValueError):
        default_max = MAX_REVIEW_ITERATIONS
    default_max = max(1, default_max)
    max_iter = state_int(state, "max_iterations", default=default_max)
    iteration = state_int(state, "iteration", default=0)

    if iteration < max_iter:
        logger.debug(
            "[HumanFlagNode] 未达迭代上限（iteration=%s < max_iterations=%s），跳过",
            iteration,
            max_iter,
        )
        return {}

    PENDING_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = PENDING_REVIEW_DIR / f"flag_{stamp}.json"

    payload: dict[str, Any] = {
        "flagged_at": stamp,
        "iteration": iteration,
        "max_iterations": max_iter,
        "review_passed": False,
        "review_feedback": str(state.get("review_feedback") or ""),
        "analyses": list(state.get("analyses") or []),
        "articles": list(state.get("articles") or []),
        "sources": list(state.get("sources") or []),
        "cost_tracker": dict(state.get("cost_tracker") or {}),
    }

    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.warning(
        "[HumanFlagNode] 已达迭代上限仍未通过审核，疑似数据或规则问题；已写入 %s",
        out_path,
    )

    return {
        "needs_human_review": True,
        "review_passed": True,
        "review_feedback": (
            "已达最大审核迭代仍未通过，条目已写入隔离目录 "
            f"knowledge/pending_review/{out_path.name}，请人工处理；主知识库不会写入本批条目。"
        ),
        "articles": [],
    }
