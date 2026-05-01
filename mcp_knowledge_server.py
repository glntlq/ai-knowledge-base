#!/usr/bin/env python3
"""MCP Knowledge Server (JSON-RPC 2.0 over stdio).

This server exposes local knowledge base articles in `knowledge/articles/` via
MCP tools so AI clients can search and retrieve content.

Protocol:
- JSON-RPC 2.0 over stdin/stdout (one JSON object per line).
- Supports methods: initialize, tools/list, tools/call

Tools:
- search_articles(keyword, limit=5)
- get_article(article_id)
- knowledge_stats()

Constraints:
- Standard library only (no third-party dependencies).
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


logger = logging.getLogger(__name__)


JsonDict = Dict[str, Any]


@dataclass
class Article:
    """Normalized article model for searching and retrieval."""

    id: str
    title: str
    summary: str
    tags: List[str]
    source: str
    raw: JsonDict
    filepath: str


class KnowledgeBase:
    """File-backed knowledge base index."""

    def __init__(self, articles_dir: Path) -> None:
        self._articles_dir = articles_dir
        self._articles: List[Article] = []
        self._by_id: Dict[str, Article] = {}

    @property
    def articles_dir(self) -> Path:
        return self._articles_dir

    def load(self) -> None:
        """Load and index all JSON articles from disk."""

        self._articles = []
        self._by_id = {}

        if not self._articles_dir.exists():
            logger.warning("articles dir not found: %s", self._articles_dir)
            return

        for fp in sorted(self._articles_dir.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - best effort load
                logger.warning("skip invalid json: %s err=%s", fp, exc)
                continue

            if not isinstance(data, dict):
                continue

            article = _normalize_article(data, fp)
            if not article.id:
                continue
            # First one wins to keep deterministic behavior.
            if article.id in self._by_id:
                continue

            self._articles.append(article)
            self._by_id[article.id] = article

        logger.info("loaded articles: %d", len(self._articles))

    def search(self, keyword: str, limit: int = 5) -> List[JsonDict]:
        """Search by keyword in title and summary (case-insensitive)."""

        kw = (keyword or "").strip().lower()
        if not kw:
            return []

        limit = max(1, min(50, int(limit)))

        scored: List[Tuple[int, Article]] = []
        for a in self._articles:
            title = a.title.lower()
            summary = a.summary.lower()
            # Simple scoring: title hit > summary hit, plus multiple occurrences.
            score = 0
            score += 5 * title.count(kw)
            score += 2 * summary.count(kw)
            if score > 0:
                scored.append((score, a))

        scored.sort(key=lambda x: (-x[0], x[1].title))
        results = []
        for score, a in scored[:limit]:
            results.append(
                {
                    "id": a.id,
                    "title": a.title,
                    "summary": a.summary,
                    "tags": a.tags,
                    "source": a.source,
                    "score": score,
                }
            )
        return results

    def get(self, article_id: str) -> Optional[JsonDict]:
        """Get full article by id."""

        aid = (article_id or "").strip()
        if not aid:
            return None
        a = self._by_id.get(aid)
        if not a:
            return None
        return a.raw

    def stats(self) -> JsonDict:
        """Compute knowledge base statistics."""

        total = len(self._articles)

        source_counter: Counter[str] = Counter()
        tag_counter: Counter[str] = Counter()

        for a in self._articles:
            source_counter[a.source] += 1
            tag_counter.update(t for t in a.tags if isinstance(t, str) and t)

        top_tags = [{"tag": t, "count": c} for t, c in tag_counter.most_common(20)]
        sources = [{"source": s, "count": c} for s, c in source_counter.most_common()]

        return {
            "total_articles": total,
            "source_distribution": sources,
            "top_tags": top_tags,
        }


def _normalize_article(data: Mapping[str, Any], fp: Path) -> Article:
    """Normalize various in-repo schemas into a stable shape."""

    article_id = str(data.get("id") or "").strip()
    title = str(data.get("title") or "").strip()
    summary = str(data.get("summary") or "").strip()

    tags_raw = data.get("tags")
    tags: List[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, str) and t.strip():
                tags.append(t.strip())

    # Support both `source` (example schema) and `source_type` (repo schema).
    source = str(data.get("source") or data.get("source_type") or "").strip()

    raw = dict(data)
    raw.setdefault("id", article_id)
    raw.setdefault("title", title)
    raw.setdefault("summary", summary)
    if "tags" not in raw:
        raw["tags"] = tags
    if "source" not in raw and source:
        raw["source"] = source

    return Article(
        id=article_id,
        title=title,
        summary=summary,
        tags=tags,
        source=source,
        raw=raw,
        filepath=str(fp),
    )


# ---------------------------------------------------------------------------
# JSON-RPC / MCP handling
# ---------------------------------------------------------------------------


def _jsonrpc_error(code: int, message: str, data: Any = None) -> JsonDict:
    err: JsonDict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


def _response(req_id: Any, result: Any = None, error: Optional[JsonDict] = None) -> JsonDict:
    resp: JsonDict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    return resp


def _tool_defs() -> List[JsonDict]:
    """Return MCP tool definitions."""

    return [
        {
            "name": "search_articles",
            "description": "按关键词搜索文章标题和摘要（大小写不敏感）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
                },
                "required": ["keyword"],
            },
        },
        {
            "name": "get_article",
            "description": "按文章 ID 获取完整 JSON 内容。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "article_id": {"type": "string", "description": "文章 ID"},
                },
                "required": ["article_id"],
            },
        },
        {
            "name": "knowledge_stats",
            "description": "返回知识库统计信息（总数、来源分布、热门标签）。",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _handle_initialize(params: Mapping[str, Any]) -> JsonDict:
    _ = params
    # Minimal MCP initialize payload.
    return {
        "protocolVersion": "2024-11-05",
        "serverInfo": {"name": "knowledge-server", "version": "0.1.0"},
        "capabilities": {
            "tools": {},
        },
    }


def _handle_tools_list() -> JsonDict:
    return {"tools": _tool_defs()}


def _handle_tools_call(kb: KnowledgeBase, params: Mapping[str, Any]) -> JsonDict:
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if not isinstance(name, str) or not name:
        raise ValueError("missing tool name")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")

    if name == "search_articles":
        keyword = str(arguments.get("keyword") or "")
        limit = arguments.get("limit", 5)
        results = kb.search(keyword=keyword, limit=int(limit))
        return {"content": [{"type": "text", "text": json.dumps(results, ensure_ascii=False, indent=2)}]}

    if name == "get_article":
        article_id = str(arguments.get("article_id") or "")
        article = kb.get(article_id=article_id)
        if article is None:
            return {"content": [{"type": "text", "text": ""}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(article, ensure_ascii=False, indent=2)}]}

    if name == "knowledge_stats":
        stats = kb.stats()
        return {"content": [{"type": "text", "text": json.dumps(stats, ensure_ascii=False, indent=2)}]}

    raise ValueError("unknown tool: %s" % name)


def serve() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    root = Path.cwd()
    articles_dir = root / "knowledge" / "articles"
    kb = KnowledgeBase(articles_dir=articles_dir)
    kb.load()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except Exception as exc:  # noqa: BLE001
            logger.warning("invalid json-rpc input: %s", exc)
            continue

        if not isinstance(req, dict):
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if "jsonrpc" not in req:
            # Not JSON-RPC 2.0, ignore.
            continue

        if not isinstance(method, str):
            out = _response(req_id, error=_jsonrpc_error(-32600, "Invalid Request"))
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        try:
            if method == "initialize":
                result = _handle_initialize(params if isinstance(params, dict) else {})
            elif method == "tools/list":
                result = _handle_tools_list()
            elif method == "tools/call":
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                result = _handle_tools_call(kb, params)
            else:
                out = _response(req_id, error=_jsonrpc_error(-32601, "Method not found"))
                sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                continue

            out = _response(req_id, result=result)
        except Exception as exc:  # noqa: BLE001
            out = _response(req_id, error=_jsonrpc_error(-32603, "Internal error", data=str(exc)))

        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(serve())

