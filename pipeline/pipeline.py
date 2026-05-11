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
  python3 -m pipeline.pipeline --limit 5 --provider qwen
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

# 用 `python pipeline/pipeline.py` 运行时，sys.path[0] 是 `pipeline/` 目录，相对导入会失败；
# 把仓库根目录放到 path 最前，统一用 `pipeline.model_client` 导入（与 `python -m pipeline.pipeline` 一致）。
_REPO_ROOT = Path(__file__).resolve().parent.parent
# 在导入 model_client 之前加载根目录 .env，供 create_provider 等读取
load_dotenv(_REPO_ROOT / ".env", override=False)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.model_client import (
    chat_with_retry,
    compute_cost_from_response,
    create_provider,
    tracker,
)

try:
    from hooks.validate_json import validate_file as _validate_article_file
except Exception:  # noqa: BLE001 - optional import for validation
    _validate_article_file = None


logger = logging.getLogger(__name__)
httpx_logger = logging.getLogger("httpx")


ROOT_DIR = _REPO_ROOT
KNOWLEDGE_DIR = ROOT_DIR / "knowledge"
RAW_DIR = KNOWLEDGE_DIR / "raw"
ARTICLES_DIR = KNOWLEDGE_DIR / "articles"
RSS_SOURCES_YAML = ROOT_DIR / "pipeline" / "rss_sources.yaml"


ISO8601_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@dataclass
class PipelineStats:
    """Pipeline run-time statistics."""

    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_total_tokens: int = 0
    llm_cost_usd: Decimal = Decimal("0")


def _step(title: str) -> None:
    logger.info("========== %s ==========", title)


def _fmt_usd(amount: Optional[Decimal]) -> str:
    if amount is None:
        return "N/A"
    return "$%s" % format(amount.quantize(Decimal("0.000001")), "f")


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
        logger.info("干跑模式：跳过写入 %s", path)
        return
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1: Collect
# ---------------------------------------------------------------------------


GITHUB_TRENDING_URL = "https://github.com/trending"


def _html_text_cleanup(value: str) -> str:
    """Convert a small HTML fragment to normalized plain text."""

    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_int_text(value: str) -> Optional[int]:
    """Parse GitHub UI numbers such as `1,234` into integers."""

    digits = re.sub(r"[^\d]", "", value or "")
    if not digits:
        return None
    return int(digits)


def _parse_github_trending_html(html_text: str, *, limit: int) -> List[Dict[str, Any]]:
    """Parse repository cards from the GitHub Trending HTML page."""

    article_re = re.compile(
        r"<article\b[^>]*class=[\"'][^\"']*Box-row[^\"']*[\"'][^>]*>(.*?)</article>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    repo_href_re = re.compile(r"<h2\b.*?</h2>", flags=re.IGNORECASE | re.DOTALL)
    href_re = re.compile(r'href=["\'](/[^/"\']+/[^/"\']+)["\']', flags=re.IGNORECASE)
    paragraph_re = re.compile(r"<p\b[^>]*>(.*?)</p>", flags=re.IGNORECASE | re.DOTALL)
    language_re = re.compile(
        r"<span\b[^>]*itemprop=[\"']programmingLanguage[\"'][^>]*>(.*?)</span>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    stars_today_re = re.compile(r"([\d,]+)\s+stars?\s+today", flags=re.IGNORECASE)

    out: List[Dict[str, Any]] = []
    for article in article_re.findall(html_text or ""):
        if len(out) >= limit:
            break

        heading = repo_href_re.search(article)
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
        description = _html_text_cleanup(description_match.group(1)) if description_match else ""

        language_match = language_re.search(article)
        language = _html_text_cleanup(language_match.group(1)) if language_match else None

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

        out.append(
            {
                "source": "github",
                "source_url": f"https://github.com/{repo_path}",
                "title": title,
                "description": description,
                "author": owner,
                "published_at": None,
                "metadata": {
                    "github_stars": _parse_int_text(stars_match.group(1)) if stars_match else None,
                    "github_language": language,
                    "github_forks": _parse_int_text(forks_match.group(1)) if forks_match else None,
                    "github_stars_today": (
                        _parse_int_text(stars_today_match.group(1)) if stars_today_match else None
                    ),
                    "github_trending_url": GITHUB_TRENDING_URL,
                },
            }
        )

    return out


def collect_github_trending(
    *,
    limit: int,
    client: httpx.Client,
) -> List[Dict[str, Any]]:
    """Collect repositories from GitHub Trending."""

    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "ai-knowledge-base",
    }
    logger.info("采集 GitHub Trending: url=%s, limit=%d", GITHUB_TRENDING_URL, limit)
    r = client.get(GITHUB_TRENDING_URL, headers=headers, follow_redirects=True)
    r.raise_for_status()

    out = _parse_github_trending_html(r.text, limit=limit)
    for item in out:
        item["collected_at"] = _utc_now_iso()
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

    logger.info("采集 RSS: %d 个源, limit=%d", len(rss_urls), limit)

    out: List[Dict[str, Any]] = []
    for feed_url in rss_urls:
        if len(out) >= limit:
            break
        feed_url = feed_url.strip()
        if not feed_url:
            continue

        try:
            r = client.get(feed_url, follow_redirects=True)
            r.raise_for_status()
            xml = r.text or ""
        except Exception as exc:  # noqa: BLE001 - tolerate individual feed failure
            logger.warning("RSS fetch failed: %s err=%s", feed_url, exc)
            continue

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


def _load_enabled_rss_urls_from_yaml(path: Path) -> List[str]:
    """Load enabled RSS URLs from pipeline/rss_sources.yaml.

    This function intentionally avoids adding a hard dependency on PyYAML at
    runtime. It tries PyYAML first (if available) and falls back to a small,
    structure-specific parser.
    """

    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")

    # Preferred: PyYAML (if installed).
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return []
        sources = data.get("sources") or []
        if not isinstance(sources, list):
            return []
        urls: List[str] = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            if not s.get("enabled"):
                continue
            url = s.get("url")
            if isinstance(url, str) and url.strip():
                urls.append(url.strip())
        return urls
    except Exception:
        pass

    # Fallback: minimal YAML-ish parser for our fixed structure.
    enabled_urls: List[str] = []
    in_item = False
    cur_enabled: Optional[bool] = None
    cur_url: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("- "):
            # Flush previous item.
            if in_item and cur_enabled and cur_url:
                enabled_urls.append(cur_url)
            in_item = True
            cur_enabled = None
            cur_url = None

        if not in_item:
            continue

        if line.startswith("url:"):
            v = line[len("url:") :].strip().strip('"').strip("'")
            cur_url = v or None
        elif line.startswith("enabled:"):
            v = line[len("enabled:") :].strip().lower()
            if v in ("true", "yes", "1"):
                cur_enabled = True
            elif v in ("false", "no", "0"):
                cur_enabled = False

    # Flush last item.
    if in_item and cur_enabled and cur_url:
        enabled_urls.append(cur_url)

    return enabled_urls


def step_collect(
    *,
    sources: Sequence[str],
    limit: int,
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Collect raw items from configured sources and persist to knowledge/raw/."""

    _step("步骤 1/4：采集（Collect）")
    _ensure_dirs(RAW_DIR)
    collected_at = _utc_now_iso()

    out: List[Dict[str, Any]] = []
    with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
        if "github" in sources:
            out.extend(collect_github_trending(limit=limit, client=client))
        if "rss" in sources:
            rss_env = (os.getenv("RSS_URLS") or "").strip()
            if rss_env:
                rss_urls = [u.strip() for u in rss_env.split(",") if u.strip()]
            else:
                rss_urls = _load_enabled_rss_urls_from_yaml(RSS_SOURCES_YAML)
            if not rss_urls:
                raise ValueError(
                    "RSS sources not configured. Set RSS_URLS or enable sources in pipeline/rss_sources.yaml"
                )
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
    logger.info("采集完成：共 %d 条", len(out))
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
    stats: Optional[PipelineStats] = None,
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

    provider_name = (os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()
    _step("步骤 2/4：分析（Analyze）")
    logger.info("创建 LLM 客户端: provider=%s, model=%s", provider_name, model)
    logger.info("待分析条目数: %d", len(items))

    out: List[Dict[str, Any]] = []
    run_stats = stats or PipelineStats()

    for idx, item in enumerate(list(items)[:limit], 1):
        messages = [
            {"role": "system", "content": "只输出严格 JSON。"},
            {"role": "user", "content": _analysis_prompt(item)},
        ]
        resp = chat_with_retry(provider=provider, messages=messages, model=model, temperature=0.2)
        cost = compute_cost_from_response(resp)
        run_stats.llm_prompt_tokens += int(resp.usage.prompt_tokens or 0)
        run_stats.llm_completion_tokens += int(resp.usage.completion_tokens or 0)
        run_stats.llm_total_tokens += int(resp.usage.total_tokens or 0)
        if cost is not None:
            run_stats.llm_cost_usd += cost

        logger.info(
            "分析进度 %d/%d | Token: %d(prompt)+%d(completion)=%d | 成本: %s | url=%s",
            idx,
            min(limit, len(items)),
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
            resp.usage.total_tokens,
            _fmt_usd(cost),
            item.get("source_url"),
        )

        parsed = _parse_json_object(resp.content)
        if parsed is None:
            logger.warning("LLM 输出不是合法 JSON，已降级为空结果。url=%s", item.get("source_url"))
            parsed = {}

        article = _merge_article_defaults(item=item, llm=parsed)
        out.append(article)

    logger.info(
        "LLM 汇总 | Token: %d(prompt)+%d(completion)=%d | 总成本: %s",
        run_stats.llm_prompt_tokens,
        run_stats.llm_completion_tokens,
        run_stats.llm_total_tokens,
        _fmt_usd(run_stats.llm_cost_usd),
    )
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

    _step("步骤 3/4：整理（Organize）")
    _ensure_dirs(ARTICLES_DIR)
    existing_urls = _read_existing_articles_source_urls()
    kept: List[Dict[str, Any]] = []

    for a in articles:
        url = str(a.get("source_url") or "").strip()
        if not url:
            logger.warning("Skip article without source_url: title=%s", a.get("title"))
            continue
        if url in existing_urls:
            logger.info("去重：已存在，跳过 url=%s (existing=%s)", url, existing_urls[url])
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
        logger.info("校验：hooks.validate_json 不可用，跳过校验步骤")
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
    _step("步骤 4/4：保存（Save）")
    _ensure_dirs(ARTICLES_DIR)

    written: List[Path] = []
    for a in articles:
        fp = ARTICLES_DIR / _article_filename(a)
        _write_json(fp, a, dry_run=dry_run)
        written.append(fp)
    logger.info("保存完成：写入 %d 篇到 %s", len(written), ARTICLES_DIR)
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
    parser.add_argument(
        "--provider",
        choices=("deepseek", "qwen", "openai"),
        default=None,
        help="覆盖环境变量 LLM_PROVIDER（未指定时仍从环境变量读取）",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run without writing files")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logs")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    # Suppress verbose httpx request logs by default.
    httpx_logger.setLevel(logging.WARNING if not args.verbose else logging.INFO)

    sources = [s.strip().lower() for s in str(args.sources).split(",") if s.strip()]
    allowed = {"github", "rss"}
    bad = [s for s in sources if s not in allowed]
    if bad:
        raise ValueError("Unsupported sources: %s" % ",".join(bad))

    if args.provider:
        os.environ["LLM_PROVIDER"] = str(args.provider).strip().lower()

    _step("知识库流水线启动")
    logger.info(
        "参数: sources=%s, limit=%d, 干跑=%s, LLM=%s",
        sources,
        args.limit,
        args.dry_run,
        (os.getenv("LLM_PROVIDER") or "deepseek").strip().lower(),
    )

    raw_items = step_collect(sources=sources, limit=int(args.limit), dry_run=bool(args.dry_run))
    if not raw_items:
        logger.warning("采集结果为空，结束。")
        return 0

    if args.dry_run:
        logger.info("干跑模式：跳过 分析/整理/保存")
        logger.info("流水线结束（干跑）。")
        return 0

    stats = PipelineStats()
    articles = analyze_items(items=raw_items, limit=int(args.limit), stats=stats)
    organized = organize_articles(articles=articles, dry_run=bool(args.dry_run))
    save_articles(articles=organized, dry_run=bool(args.dry_run))

    _step("流水线汇总")
    logger.info(
        "LLM 总 Token: %d(prompt)+%d(completion)=%d | 总成本: %s",
        stats.llm_prompt_tokens,
        stats.llm_completion_tokens,
        stats.llm_total_tokens,
        _fmt_usd(stats.llm_cost_usd),
    )
    tracker.report()
    logger.info("流水线结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

