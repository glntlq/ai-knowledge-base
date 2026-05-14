"""Collect node: fetch GitHub Trending HTML and parse repository cards."""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

from workflows.node_constants import GITHUB_TRENDING_URL
from workflows.node_support import html_text_cleanup, parse_int_text, state_int, utc_now_iso
from workflows.planner import plan_value
from workflows.state import KBState

logger = logging.getLogger(__name__)


def _urlopen_text_with_retry(
    request: urllib.request.Request,
    *,
    timeout: int,
    retries: int,
) -> str:
    """Fetch text with limited retries for transient network failures."""

    attempts = max(1, retries)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed public URL
                return response.read().decode("utf-8")
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            logger.warning(
                "[CollectNode] GitHub Trending 读取失败，准备重试 %d/%d: %s",
                attempt,
                attempts,
                exc,
            )
            time.sleep(min(2**attempt, 10))

    raise RuntimeError(
        "GitHub Trending 采集失败：请检查网络连接，或设置 "
        "GITHUB_TRENDING_TIMEOUT / GITHUB_TRENDING_RETRIES 后重试"
    ) from last_exc


def _parse_github_trending_html(html_text: str, *, limit: int) -> list[dict[str, Any]]:
    """Parse repository cards from GitHub Trending HTML."""

    article_re = re.compile(
        r"<article\b[^>]*class=[\"'][^\"']*Box-row[^\"']*[\"'][^>]*>(.*?)</article>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    heading_re = re.compile(r"<h2\b.*?</h2>", flags=re.IGNORECASE | re.DOTALL)
    href_re = re.compile(r'href=["\'](/[^/"\']+/[^/"\']+)["\']', flags=re.IGNORECASE)
    paragraph_re = re.compile(r"<p\b[^>]*>(.*?)</p>", flags=re.IGNORECASE | re.DOTALL)
    language_re = re.compile(
        r"<span\b[^>]*itemprop=[\"']programmingLanguage[\"'][^>]*>(.*?)</span>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    stars_today_re = re.compile(r"([\d,]+)\s+stars?\s+today", flags=re.IGNORECASE)

    sources = []
    for article in article_re.findall(html_text or ""):
        if len(sources) >= limit:
            break

        heading = heading_re.search(article)
        href = href_re.search(heading.group(0) if heading else article)
        if not href:
            continue

        repo_path = href.group(1).strip("/")
        if repo_path.count("/") != 1:
            continue

        owner, repo = repo_path.split("/", 1)
        title = html_text_cleanup(heading.group(0) if heading else "")
        title = re.sub(r"\s*/\s*", "/", title) or f"{owner}/{repo}"

        description_match = paragraph_re.search(article)
        language_match = language_re.search(article)
        stars_match = re.search(
            r'href=["\']/%s/stargazers["\'][^>]*>(.*?)</a>' % re.escape(repo_path),
            article,
            flags=re.IGNORECASE | re.DOTALL,
        )
        forks_match = re.search(
            r'href=["\']/%s/forks["\'][^>]*>(.*?)</a>' % re.escape(repo_path),
            article,
            flags=re.IGNORECASE | re.DOTALL,
        )
        stars_today_match = stars_today_re.search(html_text_cleanup(article))

        sources.append(
            {
                "source": "github_trending",
                "source_url": f"https://github.com/{repo_path}",
                "title": title,
                "description": (
                    html_text_cleanup(description_match.group(1))
                    if description_match
                    else ""
                ),
                "author": owner,
                "published_at": None,
                "metadata": {
                    "github_stars": (
                        parse_int_text(stars_match.group(1)) if stars_match else None
                    ),
                    "github_language": (
                        html_text_cleanup(language_match.group(1))
                        if language_match
                        else None
                    ),
                    "github_forks": (
                        parse_int_text(forks_match.group(1)) if forks_match else None
                    ),
                    "github_stars_today": (
                        parse_int_text(stars_today_match.group(1))
                        if stars_today_match
                        else None
                    ),
                    "github_trending_url": GITHUB_TRENDING_URL,
                },
            }
        )

    return sources


def collect_node(state: KBState) -> dict[str, Any]:
    """Collect repositories from GitHub Trending."""

    logger.info("[CollectNode] 采集 GitHub Trending 数据")

    env_limit = int(os.getenv("GITHUB_TRENDING_LIMIT") or 10)
    plan_limit = plan_value(state, "per_source_limit", default=None)
    try:
        default_limit = int(plan_limit) if plan_limit is not None else env_limit
    except (TypeError, ValueError):
        default_limit = env_limit
    limit = state_int(state, "limit", default=default_limit)
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "ai-knowledge-base-langgraph",
    }

    request = urllib.request.Request(GITHUB_TRENDING_URL, headers=headers)
    timeout = state_int(
        state,
        "github_trending_timeout",
        default=int(os.getenv("GITHUB_TRENDING_TIMEOUT") or 60),
    )
    retries = state_int(
        state,
        "github_trending_retries",
        default=int(os.getenv("GITHUB_TRENDING_RETRIES") or 3),
    )
    html_text = _urlopen_text_with_retry(request, timeout=timeout, retries=retries)

    collected_at = utc_now_iso()
    sources = _parse_github_trending_html(html_text, limit=limit)
    for item in sources:
        item["collected_at"] = collected_at

    return {"sources": sources}
