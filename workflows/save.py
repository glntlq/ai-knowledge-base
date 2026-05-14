"""Save node: persist articles and update index.json."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from workflows.node_support import article_filename, article_with_defaults, utc_now_iso
from workflows.state import KBState

logger = logging.getLogger(__name__)


def save_node(state: KBState) -> dict[str, Any]:
    """Persist final articles and update the article index."""

    from workflows import node_constants

    articles_dir = node_constants.ARTICLES_DIR

    logger.info("[SaveNode] 保存知识条目与索引")

    articles_dir.mkdir(parents=True, exist_ok=True)
    saved_articles = []
    for article in state.get("articles", []):
        if not isinstance(article, Mapping):
            continue
        normalized = article_with_defaults(article)
        path = articles_dir / article_filename(normalized)
        path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        saved_articles.append(normalized)

    index_payload = {
        "updated_at": utc_now_iso(),
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
    (articles_dir / "index.json").write_text(
        json.dumps(index_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {"articles": saved_articles}
