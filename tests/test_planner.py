"""Tests for workflows.planner."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class PlannerTest(unittest.TestCase):
    def test_plan_strategy_lite_tier(self) -> None:
        from workflows.planner import plan_strategy

        p = plan_strategy(5)
        self.assertEqual(p["tier"], "lite")
        self.assertEqual(p["per_source_limit"], 5)
        self.assertEqual(p["relevance_threshold"], 0.7)
        self.assertEqual(p["max_iterations"], 1)
        self.assertIn("rationale", p)
        self.assertIn("小于 10", p["rationale"])

    def test_plan_strategy_standard_tier(self) -> None:
        from workflows.planner import plan_strategy

        p = plan_strategy(10)
        self.assertEqual(p["tier"], "standard")
        self.assertEqual(p["per_source_limit"], 10)
        self.assertEqual(p["relevance_threshold"], 0.5)
        self.assertEqual(p["max_iterations"], 2)

        p19 = plan_strategy(19)
        self.assertEqual(p19["tier"], "standard")

    def test_plan_strategy_full_tier(self) -> None:
        from workflows.planner import plan_strategy

        p = plan_strategy(20)
        self.assertEqual(p["tier"], "full")
        self.assertEqual(p["per_source_limit"], 20)
        self.assertEqual(p["relevance_threshold"], 0.4)
        self.assertEqual(p["max_iterations"], 3)

    def test_plan_strategy_reads_env_when_target_none(self) -> None:
        from workflows.planner import plan_strategy

        with patch.dict(os.environ, {"PLANNER_TARGET_COUNT": "15"}, clear=False):
            p = plan_strategy(None)
        self.assertEqual(p["tier"], "standard")
        self.assertEqual(p["target_count"], 15)

    def test_planner_node_returns_plan_and_respects_state_target(self) -> None:
        from workflows.planner import planner_node

        out = planner_node({"target_count": 25, "cost_tracker": {}})  # type: ignore[arg-type]
        self.assertIn("plan", out)
        self.assertEqual(out["plan"]["tier"], "full")
        self.assertEqual(out["plan"]["target_count"], 25)

    def test_plan_value_reads_nested_plan(self) -> None:
        from workflows.planner import plan_value

        state = {"plan": {"max_iterations": 2, "tier": "standard"}}
        self.assertEqual(plan_value(state, "max_iterations", default=99), 2)
        self.assertEqual(plan_value(state, "missing", default="x"), "x")
        self.assertEqual(plan_value({}, "max_iterations", default=3), 3)
