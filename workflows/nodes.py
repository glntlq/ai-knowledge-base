"""LangGraph node functions for the knowledge base workflow."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from workflows.model_client import accumulate_usage, chat_json
from workflows.state import KBState


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
ARTICLES_DIR = ROOT_DIR / "knowledge" / "articles"
GITHUB_TRENDING_URL = "https://github.com/trending"
MAX_REVIEW_ITERATIONS = 3


def collect_node(state: KBState) -> dict[str, Any]:
    """Collect repositories from GitHub Trending."""

    logger.info("[CollectNode] 采集 GitHub Trending 数据")

    limit = _state_int(state, "limit", default=int(os.getenv("GITHUB_TRENDING_LIMIT") or 10))
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "ai-knowledge-base-langgraph",
    }

    request = urllib.request.Request(GITHUB_TRENDING_URL, headers=headers)
    timeout = _state_int(
        state,
        "github_trending_timeout",
        default=int(os.getenv("GITHUB_TRENDING_TIMEOUT") or 60),
    )
    retries = _state_int(
        state,
        "github_trending_retries",
        default=int(os.getenv("GITHUB_TRENDING_RETRIES") or 3),
    )
    html_text = _urlopen_text_with_retry(request, timeout=timeout, retries=retries)

    collected_at = _utc_now_iso()
    sources = _parse_github_trending_html(html_text, limit=limit)
    for item in sources:
        item["collected_at"] = collected_at

    return {"sources": sources}


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
        title = _html_text_cleanup(heading.group(0) if heading else "")
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
        stars_today_match = stars_today_re.search(_html_text_cleanup(article))

        sources.append(
            {
                "source": "github_trending",
                "source_url": f"https://github.com/{repo_path}",
                "title": title,
                "description": (
                    _html_text_cleanup(description_match.group(1))
                    if description_match
                    else ""
                ),
                "author": owner,
                "published_at": None,
                "metadata": {
                    "github_stars": (
                        _parse_int_text(stars_match.group(1)) if stars_match else None
                    ),
                    "github_language": (
                        _html_text_cleanup(language_match.group(1))
                        if language_match
                        else None
                    ),
                    "github_forks": (
                        _parse_int_text(forks_match.group(1)) if forks_match else None
                    ),
                    "github_stars_today": (
                        _parse_int_text(stars_today_match.group(1))
                        if stars_today_match
                        else None
                    ),
                    "github_trending_url": GITHUB_TRENDING_URL,
                },
            }
        )

    return sources


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
        analyses.append(_merge_analysis(source, llm))

    return {"analyses": analyses, "cost_tracker": tracker}


def organize_node(state: KBState) -> dict[str, Any]:
    """Filter, deduplicate, and optionally revise articles using review feedback."""

    logger.info("[OrganizeNode] 过滤、去重并整理知识条目")

    tracker = dict(state.get("cost_tracker") or {})
    candidates = [_analysis_to_article(item) for item in state.get("analyses", [])]
    articles = _dedupe_by_url(
        article for article in candidates if _quality_score(article) >= 0.6
    )

    feedback = str(state.get("review_feedback") or "").strip()
    iteration = _state_int(state, "iteration", default=0)
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
            articles = _dedupe_by_url(
                _article_with_defaults(item)
                for item in revised
                if isinstance(item, Mapping) and _quality_score(item) >= 0.6
            )

    return {"articles": articles, "cost_tracker": tracker}


def review_node(state: KBState) -> dict[str, Any]:
    """Review articles with LLM and decide whether the workflow can proceed."""

    logger.info("[ReviewNode] 审核知识条目质量")

    iteration = _state_int(state, "iteration", default=0)
    if iteration >= MAX_REVIEW_ITERATIONS - 1:
        return {
            "review_passed": True,
            "review_feedback": "已达到最大审核循环次数，强制通过并进入保存阶段。",
            "iteration": MAX_REVIEW_ITERATIONS,
        }

    tracker = dict(state.get("cost_tracker") or {})
    system = "你是知识库质检员。只输出严格 JSON，不要输出 Markdown。"
    prompt = (
        "请从四个维度审核以下知识条目：摘要质量、标签准确、分类合理、一致性。\n"
        '必须输出 JSON：{"passed": bool, "overall_score": float, '
        '"feedback": str, "scores": {"summary_quality": float, '
        '"tag_accuracy": float, "classification": float, "consistency": float}}。\n'
        "overall_score 和各维度分数范围为 0 到 1；若未通过，请给出可执行的修改建议。\n\n"
        f"知识条目：\n{json.dumps(state.get('articles', []), ensure_ascii=False, indent=2)}"
    )
    parsed, usage = chat_json(prompt, system=system)
    tracker = accumulate_usage(tracker, usage)

    result = parsed if isinstance(parsed, Mapping) else {}
    overall_score = _float_value(result.get("overall_score"), default=0.0)
    passed = bool(result.get("passed")) or overall_score >= 0.8
    feedback = str(result.get("feedback") or "").strip()

    return {
        "review_passed": passed,
        "review_feedback": feedback,
        "iteration": min(iteration + 1, MAX_REVIEW_ITERATIONS),
        "cost_tracker": tracker,
    }


def save_node(state: KBState) -> dict[str, Any]:
    """Persist final articles and update the article index."""

    logger.info("[SaveNode] 保存知识条目与索引")

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    saved_articles = []
    for article in state.get("articles", []):
        if not isinstance(article, Mapping):
            continue
        normalized = _article_with_defaults(article)
        path = ARTICLES_DIR / _article_filename(normalized)
        path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        saved_articles.append(normalized)

    index_payload = {
        "updated_at": _utc_now_iso(),
        "count": len(saved_articles),
        "articles": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                "tags": item.get("tags", []),
                "status": item.get("status"),
            }
            for item in saved_articles
        ],
    }
    (ARTICLES_DIR / "index.json").write_text(
        json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {"articles": saved_articles}


def _merge_analysis(
    source: Mapping[str, Any],
    llm: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = dict(source.get("metadata") or {})
    metadata.update(llm.get("metadata") if isinstance(llm.get("metadata"), Mapping) else {})
    quality_score = _float_value(llm.get("quality_score", metadata.get("quality_score")), 0.0)
    metadata["quality_score"] = quality_score

    return {
        "title": str(llm.get("title") or source.get("title") or "").strip(),
        "source_url": str(llm.get("source_url") or source.get("source_url") or "").strip(),
        "summary": str(llm.get("summary") or "").strip(),
        "tags": _string_list(llm.get("tags")),
        "language": str(llm.get("language") or "zh").strip().lower() or "zh",
        "author": str(source.get("author") or "").strip(),
        "published_at": source.get("published_at"),
        "collected_at": source.get("collected_at"),
        "analyzed_at": _utc_now_iso(),
        "quality_score": quality_score,
        "difficulty": str(llm.get("difficulty") or "").strip(),
        "metadata": metadata,
    }


def _analysis_to_article(analysis: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(analysis.get("metadata") or {})
    metadata["quality_score"] = _quality_score(analysis)
    article = {
        "id": analysis.get("id"),
        "title": analysis.get("title"),
        "source_url": analysis.get("source_url"),
        "source_type": analysis.get("source_type") or "github_trending",
        "summary": analysis.get("summary"),
        "content_markdown": analysis.get("content_markdown") or "",
        "tags": _string_list(analysis.get("tags")),
        "language": analysis.get("language") or "zh",
        "author": analysis.get("author") or "",
        "published_at": analysis.get("published_at"),
        "collected_at": analysis.get("collected_at"),
        "analyzed_at": analysis.get("analyzed_at") or _utc_now_iso(),
        "status": analysis.get("status") or "pending",
        "metadata": metadata,
    }
    return _article_with_defaults(article)


def _article_with_defaults(article: Mapping[str, Any]) -> dict[str, Any]:
    title = str(article.get("title") or "").strip()
    source_url = str(article.get("source_url") or "").strip()
    source_type = str(article.get("source_type") or "github_trending").strip()
    raw_id = str(article.get("id") or "").strip()
    article_id = raw_id if _is_sha256_hex(raw_id) else _stable_id(source_url, title)
    metadata = dict(article.get("metadata") or {})
    if "quality_score" not in metadata:
        metadata["quality_score"] = _quality_score(article)

    return {
        "id": article_id,
        "title": title,
        "source_url": source_url,
        "source_type": source_type,
        "summary": str(article.get("summary") or "").strip(),
        "content_markdown": str(article.get("content_markdown") or ""),
        "tags": _string_list(article.get("tags")),
        "language": str(article.get("language") or "zh").strip().lower() or "zh",
        "author": str(article.get("author") or "").strip(),
        "published_at": article.get("published_at"),
        "collected_at": article.get("collected_at") or _utc_now_iso(),
        "analyzed_at": article.get("analyzed_at") or _utc_now_iso(),
        "status": str(article.get("status") or "pending").strip().lower(),
        "metadata": metadata,
    }


def _dedupe_by_url(articles: Any) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for article in articles:
        source_url = str(article.get("source_url") or "").strip()
        key = source_url or str(article.get("id") or article.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(_article_with_defaults(article))
    return out


def _quality_score(item: Mapping[str, Any]) -> float:
    score = item.get("quality_score")
    if score is None and isinstance(item.get("metadata"), Mapping):
        score = item["metadata"].get("quality_score")
    return _float_value(score, default=0.0)


def _html_text_cleanup(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_int_text(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value or "")
    if not digits:
        return None
    return int(digits)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if score > 1 and score <= 10:
        score = score / 10
    return max(0.0, min(score, 1.0))


def _state_int(state: Mapping[str, Any], key: str, *, default: int) -> int:
    try:
        return int(state.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _article_filename(article: Mapping[str, Any]) -> str:
    date_prefix = _article_date_prefix(article)
    source_type = _safe_source_type(str(article.get("source_type") or "unknown"))

    source_url = str(article.get("source_url") or "")
    parsed = urllib.parse.urlparse(source_url)
    host_part = parsed.netloc.split(":")[0] if parsed.netloc else "item"
    slug = _slugify(str(article.get("title") or host_part))

    return f"{date_prefix}-{source_type}-{slug}.json"


def _article_date_prefix(article: Mapping[str, Any]) -> str:
    collected_at = str(article.get("collected_at") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", collected_at):
        return collected_at[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slugify(text: str, *, max_len: int = 60) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return (value[:max_len] or "item").strip("-")


def _safe_source_type(value: str) -> str:
    source_type = (value or "unknown").strip().lower()
    source_type = re.sub(r"[^a-z0-9_-]+", "-", source_type)
    source_type = re.sub(r"-{2,}", "-", source_type).strip("-_")
    return source_type[:40] or "unknown"


def _stable_id(source_url: str, title: str) -> str:
    raw = f"{source_url}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_sha256_hex(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value or ""))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
