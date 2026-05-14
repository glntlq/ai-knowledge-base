"""LangGraph assembly for the knowledge base workflow."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

from langgraph.graph import END, StateGraph

# 支持 `python workflows/graph.py` 直接执行；此时 sys.path[0] 是 workflows/。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.analyze import analyze_node
from workflows.collect import collect_node
from workflows.human_flag import human_flag_node
from workflows.node_constants import MAX_REVIEW_ITERATIONS
from workflows.node_support import state_int
from workflows.organize import organize_node
from workflows.planner import plan_value, planner_node
from workflows.reviewer import review_node
from workflows.reviser import revise_node
from workflows.save import save_node
from workflows.state import KBState


logger = logging.getLogger(__name__)


def _default_max_iterations(state: Mapping[str, Any]) -> int:
    raw = plan_value(state, "max_iterations", default=MAX_REVIEW_ITERATIONS)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return MAX_REVIEW_ITERATIONS
    return max(1, n)


def route_after_review(state: Mapping[str, Any]) -> str:
    """在 ``review`` 之后分支：通过→organize；未通过且未满迭代→revise；否则→human_flag。"""

    if bool(state.get("review_passed")):
        return "organize"

    max_iter = state_int(state, "max_iterations", default=_default_max_iterations(state))
    iteration = state_int(state, "iteration", default=0)
    if iteration < max_iter:
        return "revise"
    return "human_flag"


def build_graph() -> Any:
    """Build and compile the LangGraph knowledge base workflow."""

    graph = StateGraph(KBState)

    graph.add_node("planner", planner_node)
    graph.add_node("collect", collect_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("organize", organize_node)
    graph.add_node("review", review_node)
    graph.add_node("revise", revise_node)
    graph.add_node("human_flag", human_flag_node)
    graph.add_node("save", save_node)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "organize")
    graph.add_edge("organize", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "organize": "organize",
            "revise": "revise",
            "human_flag": "human_flag",
        },
    )
    graph.add_edge("revise", "review")
    graph.add_edge("human_flag", END)
    graph.add_edge("save", END)

    return graph.compile()


def _initial_state() -> KBState:
    return {
        "sources": [],
        "analyses": [],
        "articles": [],
        "review_feedback": "",
        "review_passed": False,
        "iteration": 0,
        "cost_tracker": {},
    }


def _summarize_value(value: Any) -> Any:
    if isinstance(value, list):
        return {"count": len(value), "sample": value[:1]}
    if isinstance(value, dict):
        keys = (
            "review_passed",
            "review_feedback",
            "iteration",
            "cost_tracker",
            "sources",
            "analyses",
            "articles",
            "needs_human_review",
            "plan",
            "target_count",
        )
        return {key: _summarize_value(value[key]) for key in keys if key in value}
    return value


def _summarize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {node_name: _summarize_value(update) for node_name, update in event.items()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    app = build_graph()
    for event in app.stream(_initial_state()):
        logger.info(
            "[GraphStream] %s",
            json.dumps(_summarize_event(event), ensure_ascii=False, indent=2),
        )
