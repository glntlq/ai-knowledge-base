#!/usr/bin/env python3
"""知识条目 JSON 文件校验脚本。

用法:
    python3 hooks/validate_json.py <json_file> [json_file2 ...]
    python3 hooks/validate_json.py "knowledge/articles/*.json"

支持单文件、多文件和通配符模式。
校验全部通过 exit 0，存在失败则 exit 1 并输出错误详情及汇总统计。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema (aligned with AGENTS.md)
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "source_type": str,
    "summary": str,
    "tags": list,
    "language": str,
    "status": str,
}

VALID_SOURCE_TYPES = frozenset({"github_trending", "hacker_news"})
VALID_LANGUAGES = frozenset({"zh", "en"})
VALID_STATUSES = frozenset({"pending", "analyzed", "distributed", "archived"})
VALID_DIFFICULTIES = frozenset({"beginner", "intermediate", "advanced"})

_URL_RE = re.compile(r"^https?://", flags=re.IGNORECASE)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$", flags=re.IGNORECASE)
# Legacy/alternate id formats seen in this repo, e.g. "20260421-github_trending-foo-bar".
_LEGACY_ID_RE = re.compile(r"^\d{8}-[a-z][a-z0-9_]*-[a-z0-9-]+$", flags=re.IGNORECASE)

MIN_SUMMARY_CHARS = 20
MIN_TAGS = 1
SCORE_MIN, SCORE_MAX = 1, 10


def _validate_id(value: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append("id 必须是字符串")
        return errors
    v = value.strip()
    if not v:
        errors.append("id 不能为空")
        return errors

    # Accept uuid v4 or sha256 hex (as documented in AGENTS.md).
    try:
        parsed = uuid.UUID(v)
        if parsed.version != 4:
            errors.append(f"id '{value}' 是 UUID 但不是 v4")
        return errors
    except (ValueError, AttributeError):
        pass

    if not _SHA256_RE.match(v):
        if not _LEGACY_ID_RE.match(v):
            errors.append("id 必须是 UUIDv4、SHA256(64位十六进制) 或 YYYYMMDD-source-slug")
    return errors


def _validate_status(value: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append("status 必须是字符串")
        return errors
    if value not in VALID_STATUSES:
        errors.append(
            f"status '{value}' 不是有效值，允许值为 {sorted(VALID_STATUSES)}"
        )
    return errors


def _validate_source_type(value: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append("source_type 必须是字符串")
        return errors
    if value not in VALID_SOURCE_TYPES:
        errors.append(
            f"source_type '{value}' 不是有效值，允许值为 {sorted(VALID_SOURCE_TYPES)}"
        )
    return errors


def _validate_language(value: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append("language 必须是字符串")
        return errors
    if value not in VALID_LANGUAGES:
        errors.append(
            f"language '{value}' 不是有效值，允许值为 {sorted(VALID_LANGUAGES)}"
        )
    return errors


def _validate_url(value: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append("source_url 必须是字符串")
        return errors
    if not _URL_RE.match(value):
        errors.append(
            f"source_url '{value}' 未以 http:// 或 https:// 开头"
        )
    return errors


def _validate_summary(value: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append("summary 必须是字符串")
        return errors
    if len(value) < MIN_SUMMARY_CHARS:
        errors.append(
            f"summary 过短（{len(value)} 字），最少需要 {MIN_SUMMARY_CHARS} 字"
        )
    return errors


def _validate_tags(value: list) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list):
        errors.append("tags 必须是列表")
        return errors
    if len(value) < MIN_TAGS:
        errors.append(f"tags 至少需要 {MIN_TAGS} 个标签")
    return errors


def _validate_score(value: Any, field_name: str = "score") -> list[str]:
    errors: list[str] = []
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(
            f"{field_name} 必须是数字，实际为 {type(value).__name__}"
        )
        return errors
    if value < SCORE_MIN or value > SCORE_MAX:
        errors.append(
            f"{field_name} 值为 {value}，超出允许范围 {SCORE_MIN}-{SCORE_MAX}"
        )
    return errors


def _validate_difficulty(value: Any, field_name: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append(
            f"{field_name} 必须是字符串，实际为 {type(value).__name__}"
        )
        return errors
    if value not in VALID_DIFFICULTIES:
        errors.append(
            f"{field_name} '{value}' 不是有效值，允许值为 {sorted(VALID_DIFFICULTIES)}"
        )
    return errors


def validate_file(filepath: Path) -> list[str]:
    """校验单个 JSON 文件，返回错误信息列表（空列表表示校验通过）。"""
    errors: list[str] = []

    if not filepath.is_file():
        return [f"文件不存在：{filepath}"]

    # --- 解析 JSON ----------------------------------------------------------
    try:
        text = filepath.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"JSON 解析失败：{exc}"]
    except OSError as exc:
        return [f"读取文件出错：{exc}"]

    if not isinstance(data, dict):
        return ["根元素必须是 JSON 对象（dict）"]

    # --- 必填字段检查 -------------------------------------------------------
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"缺少必填字段：'{field}'")
        elif not isinstance(data[field], expected_type):
            errors.append(
                f"字段 '{field}' 类型应为 {expected_type.__name__}，"
                f"实际为 {type(data[field]).__name__}"
            )

    # --- 字段专项校验 -------------------------------------------------------
    if isinstance(data.get("id"), str):
        errors.extend(_validate_id(data["id"]))
    if isinstance(data.get("status"), str):
        errors.extend(_validate_status(data["status"]))
    if isinstance(data.get("source_url"), str):
        errors.extend(_validate_url(data["source_url"]))
    if isinstance(data.get("source_type"), str):
        errors.extend(_validate_source_type(data["source_type"]))
    if isinstance(data.get("language"), str):
        errors.extend(_validate_language(data["language"]))
    if isinstance(data.get("summary"), str):
        errors.extend(_validate_summary(data["summary"]))
    if isinstance(data.get("tags"), list):
        errors.extend(_validate_tags(data["tags"]))

    # --- 可选字段：score ----------------------------------------------------
    if "score" in data:
        errors.extend(_validate_score(data["score"]))
    if isinstance(data.get("metadata"), dict) and "quality_score" in data["metadata"]:
        errors.extend(
            _validate_score(data["metadata"]["quality_score"], "metadata.quality_score")
        )

    # --- 可选字段：difficulty ----------------------------------------------
    if isinstance(data.get("metadata"), dict) and "difficulty" in data["metadata"]:
        errors.extend(
            _validate_difficulty(data["metadata"]["difficulty"], "metadata.difficulty")
        )

    return errors


def _resolve_files(paths: list[str]) -> list[Path]:
    """解析输入路径，展开通配符模式。"""
    resolved: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if any(ch in raw for ch in ("*", "?", "[")):
            if p.is_absolute():
                matched = sorted(p.parent.glob(p.name))
            else:
                matched = sorted(Path().glob(raw))
            resolved.extend(matched)
        else:
            resolved.append(p)
    return resolved


def main() -> int:
    """入口函数。全部校验通过返回 0，否则返回 1。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description="知识条目 JSON 文件校验工具")
    parser.add_argument(
        "files",
        nargs="+",
        help="JSON 文件路径，支持通配符（如 'knowledge/articles/*.json'）",
    )
    args = parser.parse_args()

    filepaths = _resolve_files(args.files)

    if not filepaths:
        logger.error("未找到匹配的文件。")
        return 1

    valid_count = 0
    invalid_count = 0
    total_errors = 0

    for fp in filepaths:
        if fp.suffix != ".json":
            logger.warning("跳过非 JSON 文件：%s", fp)
            continue

        errs = validate_file(fp)
        if errs:
            invalid_count += 1
            total_errors += len(errs)
            logger.error("未通过：%s", fp)
            for err in errs:
                logger.error("  - %s", err)
        else:
            valid_count += 1

    total = valid_count + invalid_count
    logger.info("%s", "=" * 50)
    logger.info("汇总：共检查 %d 个文件", total)
    logger.info("  通过:  %d", valid_count)
    logger.info("  未通过: %d（%d 项错误）", invalid_count, total_errors)
    logger.info("%s", "=" * 50)

    return 0 if invalid_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
