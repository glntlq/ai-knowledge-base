import io
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WorkflowNodesTest(unittest.TestCase):
    def test_collect_node_fetches_github_trending_results(self) -> None:
        from workflows.nodes import collect_node

        html = """
        <article class="Box-row">
          <h2>
            <a href="/example/agent">
              example / agent
            </a>
          </h2>
          <p>Agent framework</p>
          <span itemprop="programmingLanguage">Python</span>
          <a href="/example/agent/stargazers"> 1,234 </a>
          <a href="/example/agent/forks"> 56 </a>
          <span> 789 stars today </span>
        </article>
        """

        response = io.BytesIO(html.encode("utf-8"))
        response.__enter__ = lambda obj: obj
        response.__exit__ = lambda *args: None

        state = {"cost_tracker": {}}
        with patch("urllib.request.urlopen", return_value=response):
            update = collect_node(state)  # type: ignore[arg-type]

        self.assertEqual(update["sources"][0]["source_url"], "https://github.com/example/agent")
        self.assertEqual(update["sources"][0]["source"], "github_trending")
        self.assertEqual(update["sources"][0]["metadata"]["github_stars"], 1234)
        self.assertEqual(update["sources"][0]["metadata"]["github_stars_today"], 789)

    def test_collect_node_retries_timeout(self) -> None:
        from workflows.nodes import collect_node

        html = """
        <article class="Box-row">
          <h2><a href="/example/retry">example / retry</a></h2>
          <p>Retryable project</p>
        </article>
        """
        response = io.BytesIO(html.encode("utf-8"))
        response.__enter__ = lambda obj: obj
        response.__exit__ = lambda *args: None

        with (
            patch("urllib.request.urlopen", side_effect=[TimeoutError("slow"), response]),
            patch("time.sleep", return_value=None),
        ):
            update = collect_node({"cost_tracker": {}})  # type: ignore[arg-type]

        self.assertEqual(update["sources"][0]["source_url"], "https://github.com/example/retry")

    def test_organize_node_filters_low_score_and_deduplicates_urls(self) -> None:
        from workflows.nodes import organize_node

        state = {
            "analyses": [
                {
                    "title": "Keep",
                    "source_url": "https://example.com/a",
                    "summary": "good",
                    "quality_score": 0.8,
                    "tags": ["agent"],
                },
                {
                    "title": "Duplicate",
                    "source_url": "https://example.com/a",
                    "summary": "duplicate",
                    "quality_score": 0.9,
                    "tags": ["agent"],
                },
                {
                    "title": "Drop",
                    "source_url": "https://example.com/b",
                    "summary": "bad",
                    "quality_score": 0.5,
                    "tags": ["llm"],
                },
            ],
            "iteration": 0,
            "review_feedback": "",
            "cost_tracker": {},
        }

        update = organize_node(state)  # type: ignore[arg-type]

        self.assertEqual(len(update["articles"]), 1)
        self.assertEqual(update["articles"][0]["source_url"], "https://example.com/a")

    def test_review_node_forces_pass_after_two_iterations(self) -> None:
        from workflows.nodes import review_node

        state = {
            "articles": [{"title": "A", "source_url": "https://example.com/a"}],
            "iteration": 2,
            "cost_tracker": {},
        }

        update = review_node(state)  # type: ignore[arg-type]

        self.assertTrue(update["review_passed"])
        self.assertEqual(update["iteration"], 3)

    def test_save_node_writes_articles_and_index(self) -> None:
        from workflows import nodes

        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir)

        with patch.object(nodes, "ARTICLES_DIR", Path(temp_dir)):
            update = nodes.save_node(
                {
                    "articles": [
                        {
                            "id": "article-1",
                            "title": "Article 1",
                            "source_url": "https://example.com/a",
                            "summary": "summary",
                            "tags": ["agent"],
                        }
                    ]
                }  # type: ignore[arg-type]
            )

        expected_id = hashlib.sha256(
            "https://example.com/a|Article 1".encode("utf-8")
        ).hexdigest()
        self.assertEqual(len(update["articles"]), 1)
        self.assertEqual(update["articles"][0]["id"], expected_id)
        self.assertTrue((Path(temp_dir) / f"{expected_id}.json").exists())
        self.assertTrue((Path(temp_dir) / "index.json").exists())


if __name__ == "__main__":
    unittest.main()
