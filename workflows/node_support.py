"""Shared helpers for workflow nodes (not LangGraph entrypoints)."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Mapping


def html_text_cleanup(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_int_text(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value or "")
    if not digits:
        return None
    return int(digits)


def float_value(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if score > 1 and score <= 10:
        score = score / 10
    return max(0.0, min(score, 1.0))


def state_int(state: Mapping[str, Any], key: str, *, default: int) -> int:
    try:
        return int(state.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def merge_analysis(source: Mapping[str, Any], llm: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(source.get("metadata") or {})
    metadata.update(llm.get("metadata") if isinstance(llm.get("metadata"), Mapping) else {})
    q_score = float_value(llm.get("quality_score", metadata.get("quality_score")), 0.0)
    metadata["quality_score"] = q_score

    return {
        "title": str(llm.get("title") or source.get("title") or "").strip(),
        "source_url": str(llm.get("source_url") or source.get("source_url") or "").strip(),
        "summary": str(llm.get("summary") or "").strip(),
        "tags": string_list(llm.get("tags")),
        "language": str(llm.get("language") or "zh").strip().lower() or "zh",
        "author": str(source.get("author") or "").strip(),
        "published_at": source.get("published_at"),
        "collected_at": source.get("collected_at"),
        "analyzed_at": utc_now_iso(),
        "quality_score": q_score,
        "difficulty": str(llm.get("difficulty") or "").strip(),
        "metadata": metadata,
    }


def quality_score(item: Mapping[str, Any]) -> float:
    score = item.get("quality_score")
    if score is None and isinstance(item.get("metadata"), Mapping):
        score = item["metadata"].get("quality_score")
    return float_value(score, default=0.0)


def is_sha256_hex(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value or ""))


def stable_id(source_url: str, title: str) -> str:
    raw = f"{source_url}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def article_with_defaults(article: Mapping[str, Any]) -> dict[str, Any]:
    title = str(article.get("title") or "").strip()
    source_url = str(article.get("source_url") or "").strip()
    source_type = str(article.get("source_type") or "github_trending").strip()
    raw_id = str(article.get("id") or "").strip()
    article_id = raw_id if is_sha256_hex(raw_id) else stable_id(source_url, title)
    metadata = dict(article.get("metadata") or {})
    if "quality_score" not in metadata:
        metadata["quality_score"] = quality_score(article)

    return {
        "id": article_id,
        "title": title,
        "source_url": source_url,
        "source_type": source_type,
        "summary": str(article.get("summary") or "").strip(),
        "content_markdown": str(article.get("content_markdown") or ""),
        "tags": string_list(article.get("tags")),
        "language": str(article.get("language") or "zh").strip().lower() or "zh",
        "author": str(article.get("author") or "").strip(),
        "published_at": article.get("published_at"),
        "collected_at": article.get("collected_at") or utc_now_iso(),
        "analyzed_at": article.get("analyzed_at") or utc_now_iso(),
        "status": str(article.get("status") or "pending").strip().lower(),
        "metadata": metadata,
    }


def analysis_to_article(analysis: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(analysis.get("metadata") or {})
    metadata["quality_score"] = quality_score(analysis)
    article = {
        "id": analysis.get("id"),
        "title": analysis.get("title"),
        "source_url": analysis.get("source_url"),
        "source_type": analysis.get("source_type") or "github_trending",
        "summary": analysis.get("summary"),
        "content_markdown": analysis.get("content_markdown") or "",
        "tags": string_list(analysis.get("tags")),
        "language": analysis.get("language") or "zh",
        "author": analysis.get("author") or "",
        "published_at": analysis.get("published_at"),
        "collected_at": analysis.get("collected_at"),
        "analyzed_at": analysis.get("analyzed_at") or utc_now_iso(),
        "status": analysis.get("status") or "pending",
        "metadata": metadata,
    }
    return article_with_defaults(article)


def dedupe_by_url(articles: Any) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for article in articles:
        source_url = str(article.get("source_url") or "").strip()
        key = source_url or str(article.get("id") or article.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(article_with_defaults(article))
    return out


def slugify(text: str, *, max_len: int = 60) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return (value[:max_len] or "item").strip("-")


def safe_source_type(value: str) -> str:
    source_type = (value or "unknown").strip().lower()
    source_type = re.sub(r"[^a-z0-9_-]+", "-", source_type)
    source_type = re.sub(r"-{2,}", "-", source_type).strip("-_")
    return source_type[:40] or "unknown"


def article_date_prefix(article: Mapping[str, Any]) -> str:
    collected_at = str(article.get("collected_at") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", collected_at):
        return collected_at[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def article_filename(article: Mapping[str, Any]) -> str:
    date_prefix = article_date_prefix(article)
    source_type = safe_source_type(str(article.get("source_type") or "unknown"))

    source_url = str(article.get("source_url") or "")
    parsed = urllib.parse.urlparse(source_url)
    host_part = parsed.netloc.split(":")[0] if parsed.netloc else "item"
    slug = slugify(str(article.get("title") or host_part))

    return f"{date_prefix}-{source_type}-{slug}.json"
