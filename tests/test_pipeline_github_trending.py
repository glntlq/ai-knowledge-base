import unittest


from pipeline.pipeline import _parse_github_trending_html


class GitHubTrendingParserTest(unittest.TestCase):
    def test_extracts_repo_cards(self) -> None:
        html = """
        <article class="Box-row">
          <h2>
            <a href="/owner-name/repo-name">
              owner-name / repo-name
            </a>
          </h2>
          <p class="col-9 color-fg-muted my-1 pr-4">
            Useful AI agent framework.
          </p>
          <span itemprop="programmingLanguage">Python</span>
          <a href="/owner-name/repo-name/stargazers"> 1,234 </a>
          <a href="/owner-name/repo-name/forks"> 56 </a>
          <span class="d-inline-block float-sm-right"> 789 stars today </span>
        </article>
        """

        items = _parse_github_trending_html(html, limit=1)

        self.assertEqual(
            items,
            [
                {
                    "source": "github",
                    "source_url": "https://github.com/owner-name/repo-name",
                    "title": "owner-name/repo-name",
                    "description": "Useful AI agent framework.",
                    "author": "owner-name",
                    "published_at": None,
                    "metadata": {
                        "github_stars": 1234,
                        "github_language": "Python",
                        "github_forks": 56,
                        "github_stars_today": 789,
                        "github_trending_url": "https://github.com/trending",
                    },
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
