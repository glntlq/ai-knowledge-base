"""Knowledge base automation pipeline.

Pipeline stages:
1. Collect  - GitHub Search API + RSS sources (regex-based parsing).
2. Analyze  - Call LLM to summarize/score/tag.
3. Organize - Deduplicate + normalize + validate.
4. Save    - Persist raw data to knowledge/raw/ and final articles to knowledge/articles/.

CLI examples:
  python3 pipeline/pipeline.py --sources github,rss --limit 20
  python3 pipeline/pipeline.py --sources github --limit 5
  python3 pipeline/pipeline.py --sources rss --limit 10
  python3 pipeline/pipeline.py --sources github --limit 5 --dry-run
  python3 pipeline/pipeline.py --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx

try:
    # When executed as a module: python -m pipeline.pipeline
    from .model_client import chat_with_retry, create_provider
except Exception:  # noqa: BLE001
    # When executed as a script: python3 pipeline/pipeline.py
    from model_client import chat_with_retry, create_provider  # type: ignore

try:
    from hooks.validate_json import validate_file as _validate_article_file
except Exception:  # noqa: BLE001 - optional import for validation
    _validate_article_file = None


logger = logging.getLogger(__name__)


ROOT_DIR = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = ROOT_DIR / "knowledge"
RAW_DIR = KNOWLEDGE_DIR / "raw"
ARTICLES_DIR = KNOWLEDGE_DIR / "articles"


ISO8601_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _slugify(text: str, *, max_len: int = 60) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len] or "item").strip("-")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_existing_articles_source_urls() -> Dict[str, str]:
    """Build a map: source_url -> filepath for dedupe."""

    existing: Dict[str, str] = {}
    if not ARTICLES_DIR.exists():
        return existing

    for fp in ARTICLES_DIR.glob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        url = data.get("source_url")
        if isinstance(url, str) and url.strip():
            existing[url.strip()] = str(fp)
    return existing


def _write_json(path: Path, data: Any, *, dry_run: bool) -> None:
    if dry_run:
        logger.info("dry-run: skip writing %s", path)
        return
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1: Collect
# ---------------------------------------------------------------------------


def collect_github_search(
    *,
    limit: int,
    client: httpx.Client,
) -> List[Dict[str, Any]]:
    """Collect AI-related repos via GitHub Search API.

    Uses env:
      - GITHUB_TOKEN (optional, recommended to avoid strict rate-limits)
      - GITHUB_SEARCH_QUERY (optional)
    """

    token = (os.getenv("GITHUB_TOKEN") or "").strip()
    query = (os.getenv("GITHUB_SEARCH_QUERY") or "AI OR LLM OR agent OR RAG").strip()

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-knowledge-base",
    }
    if token:
        headers["Authorization"] = "Bearer %s" % token

    url = "https://api.github.com/search/repositories"

    per_page = min(100, max(1, limit))
    params = {
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": per_page,
        "page": 1,
    }

    logger.info("Collect GitHub: q=%s limit=%d", query, limit)
    r = client.get(url, headers=headers, params=params)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("items") or []
    out: List[Dict[str, Any]] = []

    for it in items[:limit]:
        out.append(
            {
                "source": "github",
                "source_url": it.get("html_url"),
                "title": it.get("full_name") or it.get("name") or "",
                "description": it.get("description") or "",
                "author": (it.get("owner") or {}).get("login") if isinstance(it.get("owner"), dict) else "",
                "published_at": it.get("created_at") or None,
                "metadata": {
                    "github_stars": it.get("stargazers_count"),
                    "github_language": it.get("language"),
                    "github_forks": it.get("forks_count"),
                    "github_open_issues": it.get("open_issues_count"),
                },
                "collected_at": _utc_now_iso(),
            }
        )
    return out


_RSS_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", flags=re.IGNORECASE | re.DOTALL)
_RSS_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", flags=re.IGNORECASE | re.DOTALL)
_RSS_LINK_RE = re.compile(r"<link\b[^>]*>(.*?)</link>", flags=re.IGNORECASE | re.DOTALL)
_RSS_PUBDATE_RE = re.compile(r"<pubDate\b[^>]*>(.*?)</pubDate>", flags=re.IGNORECASE | re.DOTALL)
_RSS_DESC_RE = re.compile(r"<description\b[^>]*>(.*?)</description>", flags=re.IGNORECASE | re.DOTALL)


def _rss_text_cleanup(s: str) -> str:
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.DOTALL)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&quot;", '"').replace("&#39;", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def collect_rss(
    *,
    limit: int,
    client: httpx.Client,
    rss_urls: Sequence[str],
) -> List[Dict[str, Any]]:
    """Collect RSS items using regex parsing (intentionally lightweight)."""

    logger.info("Collect RSS: %d feed(s), limit=%d", len(rss_urls), limit)

    out: List[Dict[str, Any]] = []
    for feed_url in rss_urls:
        if len(out) >= limit:
            break
        feed_url = feed_url.strip()
        if not feed_url:
            continue

        r = client.get(feed_url, follow_redirects=True)
        r.raise_for_status()
        xml = r.text or ""

        for item_xml in _RSS_ITEM_RE.findall(xml):
            if len(out) >= limit:
                break

            title_m = _RSS_TITLE_RE.search(item_xml)
            link_m = _RSS_LINK_RE.search(item_xml)
            pub_m = _RSS_PUBDATE_RE.search(item_xml)
            desc_m = _RSS_DESC_RE.search(item_xml)

            title = _rss_text_cleanup(title_m.group(1)) if title_m else ""
            link = _rss_text_cleanup(link_m.group(1)) if link_m else ""
            pub = _rss_text_cleanup(pub_m.group(1)) if pub_m else None
            desc = _rss_text_cleanup(desc_m.group(1)) if desc_m else ""

            if not link or not title:
                continue

            out.append(
                {
                    "source": "rss",
                    "source_url": link,
                    "title": title,
                    "description": desc,
                    "author": "",
                    "published_at": pub,
                    "metadata": {"rss_feed": feed_url},
                    "collected_at": _utc_now_iso(),
                }
            )

    return out[:limit]


def step_collect(
    *,
    sources: Sequence[str],
    limit: int,
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Collect raw items from configured sources and persist to knowledge/raw/."""

    _ensure_dirs(RAW_DIR)
    collected_at = _utc_now_iso()

    out: List[Dict[str, Any]] = []
    with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
        if "github" in sources:
            out.extend(collect_github_search(limit=limit, client=client))
        if "rss" in sources:
            rss_env = (os.getenv("RSS_URLS") or "").strip()
            if not rss_env:
                raise ValueError("RSS_URLS is required when --sources includes rss")
            rss_urls = [u.strip() for u in rss_env.split(",") if u.strip()]
            out.extend(collect_rss(limit=limit, client=client, rss_urls=rss_urls))

    # Trim to limit if user provided multiple sources (we keep order by source).
    out = out[:limit]

    raw_name = "raw-%s-%s.json" % (
        "-".join(sorted(sources)),
        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    raw_path = RAW_DIR / raw_name
    raw_payload = {
        "collected_at": collected_at,
        "sources": list(sources),
        "limit": limit,
        "items": out,
    }
    _write_json(raw_path, raw_payload, dry_run=dry_run)
    logger.info("Collected %d item(s)", len(out))
    return out


# ---------------------------------------------------------------------------
# Step 2: Analyze
# ---------------------------------------------------------------------------


def _analysis_prompt(item: Mapping[str, Any]) -> str:
    title = str(item.get("title") or "")
    url = str(item.get("source_url") or "")
    desc = str(item.get("description") or "")
    source = str(item.get("source") or "")

    return (
        "你是 AI 知识库助手。请基于给定内容生成最终知识条目 JSON（仅输出 JSON，不要输出多余文本）。\n"
        "要求：\n"
        "- summary: 200-300 字中文，高信息密度，避免空洞词\n"
        "- tags: 3-5 个，尽量使用通用技术标签（小写、连字符）\n"
        "- language: zh 或 en\n"
        "- metadata.quality_score: 1-10\n"
        "- metadata.difficulty: beginner/intermediate/advanced\n"
        "- status: pending\n"
        "- source_type: github_trending 或 hacker_news（如果是 github 搜索结果也用 github_trending；rss 视为 hacker_news）\n"
        "- 时间字段尽量补全：collected_at / analyzed_at；published_at 若未知可为 null\n"
        "\n"
        "输入：\n"
        "source=%s\n"
        "title=%s\n"
        "url=%s\n"
        "description=%s\n"
    ) % (source, title, url, desc)


def _coerce_source_type(source: str) -> str:
    # Keep schema-compatible source_type.
    if source == "github":
        return "github_trending"
    return "hacker_news"


def analyze_items(
    *,
    items: Sequence[Mapping[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """Analyze items with LLM and return article dicts."""

    provider = create_provider()
    model = (os.getenv("LLM_MODEL") or "").strip()
    if not model:
        # Reasonable defaults per provider.
        provider_name = (os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()
        if provider_name == "deepseek":
            model = "deepseek-chat"
        elif provider_name == "qwen":
            model = "qwen-turbo"
        else:
            model = "gpt-4o-mini"

    logger.info("Analyze: provider=%s model=%s items=%d", os.getenv("LLM_PROVIDER") or "deepseek", model, len(items))

    out: List[Dict[str, Any]] = []
    for item in list(items)[:limit]:
        messages = [
            {"role": "system", "content": "只输出严格 JSON。"},
            {"role": "user", "content": _analysis_prompt(item)},
        ]
        resp = chat_with_retry(provider=provider, messages=messages, model=model, temperature=0.2)
        parsed = _parse_json_object(resp.content)
        if parsed is None:
            logger.warning("LLM output is not valid JSON, fallback minimal. url=%s", item.get("source_url"))
            parsed = {}

        article = _merge_article_defaults(item=item, llm=parsed)
        out.append(article)
    return out


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return None
    # Best effort: trim fenced blocks.
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _merge_article_defaults(*, item: Mapping[str, Any], llm: Mapping[str, Any]) -> Dict[str, Any]:
    """Standardize into the repo's article schema."""

    source = str(item.get("source") or "")
    source_type = _coerce_source_type(source)

    title = str(llm.get("title") or item.get("title") or "").strip()
    source_url = str(llm.get("source_url") or item.get("source_url") or "").strip()
    summary = str(llm.get("summary") or "").strip()
    tags = llm.get("tags") if isinstance(llm.get("tags"), list) else []

    language = str(llm.get("language") or "zh").strip().lower()
    status = str(llm.get("status") or "pending").strip().lower()

    metadata = llm.get("metadata") if isinstance(llm.get("metadata"), dict) else {}
    merged_meta = dict(item.get("metadata") or {})
    merged_meta.update(metadata)

    collected_at = str(item.get("collected_at") or _utc_now_iso())

    published_at = llm.get("published_at", item.get("published_at"))
    if published_at is not None and not isinstance(published_at, str):
        published_at = str(published_at)

    analyzed_at = llm.get("analyzed_at") or _utc_now_iso()

    # id strategy: stable sha256 (url + title) to avoid duplicates across runs.
    stable_key = "%s|%s|%s" % (source_type, source_url, title)
    article_id = str(llm.get("id") or _sha256_hex(stable_key))

    author = str(llm.get("author") or item.get("author") or "").strip()

    return {
        "id": article_id,
        "title": title,
        "source_url": source_url,
        "source_type": source_type,
        "summary": summary,
        "content_markdown": llm.get("content_markdown"),
        "tags": _normalize_tags(tags),
        "language": "zh" if language not in ("zh", "en") else language,
        "author": author,
        "published_at": published_at,
        "collected_at": collected_at,
        "analyzed_at": analyzed_at,
        "status": status if status else "pending",
        "metadata": merged_meta,
    }


def _normalize_tags(tags: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for t in tags:
        if not isinstance(t, str):
            continue
        s = t.strip().lower().replace(" ", "-")
        s = re.sub(r"[^a-z0-9-]+", "", s)
        s = re.sub(r"-{2,}", "-", s).strip("-")
        if s:
            out.append(s)
    # unique preserving order
    seen = set()  # type: Set[str]
    uniq: List[str] = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq[:8]


# ---------------------------------------------------------------------------
# Step 3: Organize
# ---------------------------------------------------------------------------


def organize_articles(
    *,
    articles: Sequence[Dict[str, Any]],
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Deduplicate, normalize, and validate in-memory article dicts."""

    _ensure_dirs(ARTICLES_DIR)
    existing_urls = _read_existing_articles_source_urls()
    kept: List[Dict[str, Any]] = []

    for a in articles:
        url = str(a.get("source_url") or "").strip()
        if not url:
            logger.warning("Skip article without source_url: title=%s", a.get("title"))
            continue
        if url in existing_urls:
            logger.info("Dedup skip existing url=%s (existing=%s)", url, existing_urls[url])
            continue

        # Ensure required fields exist with sane defaults.
        a.setdefault("status", "pending")
        a.setdefault("language", "zh")
        a.setdefault("tags", [])
        a.setdefault("metadata", {})
        a.setdefault("collected_at", _utc_now_iso())
        a.setdefault("analyzed_at", _utc_now_iso())

        kept.append(a)

    # Optional validation by writing to temp and calling validate_file() if available.
    if _validate_article_file is None:
        logger.info("Validation: hooks.validate_json not available, skip validate step")
        return kept

    validated: List[Dict[str, Any]] = []
    for a in kept:
        temp = ARTICLES_DIR / (".tmp-validate-%s.json" % _slugify(str(a.get("id") or "item"), max_len=24))
        try:
            _write_json(temp, a, dry_run=dry_run)
            if dry_run:
                validated.append(a)
                continue
            errs = _validate_article_file(temp)
            if errs:
                logger.error("Validation failed for url=%s", a.get("source_url"))
                for e in errs:
                    logger.error("  - %s", e)
                continue
            validated.append(a)
        finally:
            if temp.exists() and not dry_run:
                try:
                    temp.unlink()
                except Exception:  # noqa: BLE001
                    pass
    return validated


# ---------------------------------------------------------------------------
# Step 4: Save
# ---------------------------------------------------------------------------


def _article_filename(article: Mapping[str, Any]) -> str:
    date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source_type = str(article.get("source_type") or "unknown")

    slug_source = str(article.get("source_url") or "")
    parsed = urlparse(slug_source)
    host_part = parsed.netloc.split(":")[0] if parsed.netloc else "item"
    slug = _slugify(str(article.get("title") or host_part))
    return "%s-%s-%s.json" % (date_prefix, source_type, slug)


def save_articles(
    *,
    articles: Sequence[Dict[str, Any]],
    dry_run: bool,
) -> List[Path]:
    _ensure_dirs(ARTICLES_DIR)

    written: List[Path] = []
    for a in articles:
        fp = ARTICLES_DIR / _article_filename(a)
        _write_json(fp, a, dry_run=dry_run)
        written.append(fp)
    logger.info("Saved %d article(s) to %s", len(written), ARTICLES_DIR)
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Knowledge base automation pipeline")
    parser.add_argument(
        "--sources",
        default="github,rss",
        help="Comma-separated sources: github,rss",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max items to process")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing files")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logs")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    sources = [s.strip().lower() for s in str(args.sources).split(",") if s.strip()]
    allowed = {"github", "rss"}
    bad = [s for s in sources if s not in allowed]
    if bad:
        raise ValueError("Unsupported sources: %s" % ",".join(bad))

    logger.info("Pipeline start: sources=%s limit=%d dry_run=%s", sources, args.limit, args.dry_run)

    raw_items = step_collect(sources=sources, limit=int(args.limit), dry_run=bool(args.dry_run))
    if not raw_items:
        logger.warning("No items collected.")
        return 0

    if args.dry_run:
        logger.info("dry-run: skip Analyze/Organize/Save stages")
        logger.info("Pipeline done (dry-run).")
        return 0

    articles = analyze_items(items=raw_items, limit=int(args.limit))
    organized = organize_articles(articles=articles, dry_run=bool(args.dry_run))
    save_articles(articles=organized, dry_run=bool(args.dry_run))

    logger.info("Pipeline done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

