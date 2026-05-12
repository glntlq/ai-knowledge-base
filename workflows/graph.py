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

from workflows.nodes import (
    analyze_node,
    collect_node,
    organize_node,
    review_node,
    save_node,
)
from workflows.state import KBState


logger = logging.getLogger(__name__)


def route_after_review(state: Mapping[str, Any]) -> str:
    """Route to save when review passes, otherwise loop back to organize."""

    return "save" if bool(state.get("review_passed")) else "organize"


def build_graph() -> Any:
    """Build and compile the LangGraph knowledge base workflow."""

    graph = StateGraph(KBState)

    graph.add_node("collect", collect_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("organize", organize_node)
    graph.add_node("review", review_node)
    graph.add_node("save", save_node)

    graph.set_entry_point("collect")
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "organize")
    graph.add_edge("organize", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "save": "save",
            "organize": "organize",
        },
    )
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
