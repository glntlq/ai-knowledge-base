import subprocess
import sys
import unittest
from pathlib import Path


class WorkflowGraphTest(unittest.TestCase):
    def test_build_graph_returns_compiled_app(self) -> None:
        from workflows.graph import build_graph

        app = build_graph()

        self.assertTrue(callable(getattr(app, "stream", None)))
        self.assertTrue(callable(getattr(app, "invoke", None)))

    def test_review_router_branches(self) -> None:
        from workflows.graph import route_after_review
        from workflows.node_constants import MAX_REVIEW_ITERATIONS

        self.assertEqual(
            route_after_review({"review_passed": True, "iteration": 0}),
            "organize",
        )
        self.assertEqual(
            route_after_review({"review_passed": False, "iteration": 0}),
            "revise",
        )
        self.assertEqual(
            route_after_review(
                {"review_passed": False, "iteration": MAX_REVIEW_ITERATIONS - 1}
            ),
            "revise",
        )
        self.assertEqual(
            route_after_review(
                {"review_passed": False, "iteration": MAX_REVIEW_ITERATIONS}
            ),
            "human_flag",
        )

    def test_graph_file_imports_when_loaded_like_direct_script(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        graph_path = repo_root / "workflows" / "graph.py"
        code = (
            "import importlib.util, sys\n"
            f"repo_root = {str(repo_root)!r}\n"
            f"graph_path = {str(graph_path)!r}\n"
            "sys.path = [repo_root + '/workflows'] + [p for p in sys.path if p not in ('', repo_root)]\n"
            "spec = importlib.util.spec_from_file_location('graph_direct_import', graph_path)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            cwd=repo_root / "workflows",
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
