#!/usr/bin/env python3
"""知识条目 5 维度质量评分脚本。

用法:
    python3 hooks/check_quality.py <json_file> [json_file2 ...]
    python3 hooks/check_quality.py "knowledge/articles/*.json"

5 个评分维度（加权总分 100 分）:
    - 摘要质量 (25 分): >= 50 字满分，>= 20 字基本分，含技术关键词有奖励
    - 技术深度 (25 分): 基于 score/quality_score 字段（1-10 映射到 0-25）
    - 格式规范 (20 分): id、title、source_url、status、时间戳五项各 4 分
    - 标签精度 (15 分): 1-3 个合法标签最佳，有标准标签列表校验
    - 空洞词检测 (15 分): 不含"赋能""抓手""闭环""打通"等空洞词

等级标准: A >= 80, B >= 60, C < 60
存在 C 级条目时 exit 1，全部 A/B 时 exit 0。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

TECH_KEYWORDS: frozenset[str] = frozenset({
    "llm", "agent", "rag", "transformer", "fine-tuning", "fine tuning",
    "prompt", "embedding", "vector", "retrieval", "langchain",
    "llamaindex", "openai", "anthropic", "claude", "gpt", "bert",
    "diffusion", "neural", "reinforcement", "machine learning",
    "deep learning", "nlp", "attention", "token", "inference",
    "training", "dataset", "benchmark", "open source", "api", "sdk",
    "framework", "pipeline", "orchestration", "multi-modal",
    "multimodal", "vision", "speech", "text-to", "chat",
    "conversation", "tool", "function calling", "plugin", "memory",
    "context", "knowledge graph", "graph", "python", "golang",
    "rust", "typescript", "javascript",
})

STANDARD_TAGS: frozenset[str] = frozenset({
    "llm", "agent", "rag", "python", "machine-learning", "deep-learning",
    "nlp", "computer-vision", "reinforcement-learning", "transformer",
    "fine-tuning", "prompt-engineering", "tool-use", "code-generation",
    "vector-database", "embedding", "langchain", "open-source", "api",
    "cli", "framework", "chatbot", "multimodal", "quantitative-finance",
    "ai-trading", "investment", "data-analysis", "visualization",
    "inference", "model-deployment", "evaluation", "benchmark",
    "text-generation", "image-generation", "speech-recognition",
    "document-processing", "knowledge-graph", "search", "retrieval",
    "safety", "alignment", "distillation", "quantization",
})

BUZZWORD_CN: frozenset[str] = frozenset({
    "赋能", "抓手", "闭环", "打通", "全链路", "底层逻辑",
    "颗粒度", "对齐", "拉通", "沉淀", "强大的", "革命性的",
})

BUZZWORD_EN: frozenset[str] = frozenset({
    "groundbreaking", "revolutionary", "game-changing", "cutting-edge",
    "best-in-class", "world-class", "next-generation", "state-of-the-art",
    "paradigm-shift", "synergy", "disruptive", "innovative",
    "holistic", "seamless", "robust", "scalable",
})

VALID_STATUSES: frozenset[str] = frozenset({
    "pending", "analyzed", "distributed", "archived", "draft", "review",
    "published",
})


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class DimensionScore:
    """单个维度的评分详情。"""

    name: str
    max_score: int
    score: int
    details: list[str] = field(default_factory=list)


@dataclass
class QualityReport:
    """单条知识条目的完整质量报告。"""

    filepath: Path
    total_score: int
    grade: str
    dimensions: list[DimensionScore]
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 评分函数
# ---------------------------------------------------------------------------


def _score_summary(data: dict[str, Any]) -> DimensionScore:
    """摘要质量评分（满分 25 分）。"""
    summary = data.get("summary", "")
    details: list[str] = []

    if not summary or not isinstance(summary, str):
        return DimensionScore("摘要质量", 25, 0, ["摘要为空或非字符串"])

    length = len(summary)

    if length >= 50:
        base = 20
        details.append(f"摘要长度 {length} 字 (>= 50) -> 基础分 {base}")
    elif length >= 20:
        base = 15
        details.append(f"摘要长度 {length} 字 (>= 20) -> 基础分 {base}")
    else:
        base = max(5, int(length / 20 * 15))
        details.append(f"摘要长度 {length} 字 (< 20) -> 基础分 {base}")

    summary_lower = summary.lower()
    found_keywords = [kw for kw in sorted(TECH_KEYWORDS) if kw in summary_lower]
    bonus = min(5, len(found_keywords) * 2)
    if bonus > 0:
        preview = ", ".join(found_keywords[:6])
        suffix = f" 等共 {len(found_keywords)} 个" if len(found_keywords) > 6 else ""
        details.append(f"技术关键词: {preview}{suffix} -> 奖励 {bonus} 分")

    total = min(25, base + bonus)
    return DimensionScore("摘要质量", 25, total, details)


def _extract_score(data: dict[str, Any]) -> int | float | None:
    """从 JSON 数据中提取 score 字段的值。"""
    score = data.get("score")
    if score is None and isinstance(data.get("metadata"), dict):
        score = data["metadata"].get("quality_score")
    return score


def _score_depth(data: dict[str, Any]) -> DimensionScore:
    """技术深度评分（满分 25 分）—— 基于 score 字段 1-10 映射到 0-25。"""
    details: list[str] = []
    score = _extract_score(data)

    if score is None:
        return DimensionScore("技术深度", 25, 0, ["缺少 score 或 metadata.quality_score 字段"])

    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return DimensionScore(
            "技术深度", 25, 0,
            [f"score 类型错误: {type(score).__name__}"],
        )

    raw = float(score)
    if raw < 1 or raw > 10:
        details.append(f"score={raw} 超出 1-10 范围，已截断")
        raw = max(1.0, min(10.0, raw))

    mapped = min(25, max(0, int(raw * 2.5)))
    details.append(f"score={raw}/10 -> {mapped}/25")
    return DimensionScore("技术深度", 25, mapped, details)


def _score_format(data: dict[str, Any]) -> DimensionScore:
    """格式规范评分（满分 20 分）—— 5 项各 4 分。"""
    details: list[str] = []
    earned = 0

    field_checks = [
        ("id", data.get("id")),
        ("title", data.get("title")),
        ("source_url", data.get("source_url")),
    ]
    for field_name, value in field_checks:
        if isinstance(value, str) and value.strip():
            details.append(f"{field_name} 存在且非空 -> +4")
            earned += 4
        else:
            details.append(f"{field_name} 缺失或为空 -> +0")

    # status：额外校验合法性
    status = data.get("status")
    if isinstance(status, str) and status.strip():
        if status in VALID_STATUSES:
            details.append(f"status='{status}' 合法 -> +4")
            earned += 4
        else:
            details.append(f"status='{status}' 不在有效状态列表中 -> +0")
    else:
        details.append("status 缺失或为空 -> +0")

    # 时间戳：至少一项非空
    timestamps = (
        data.get("published_at"),
        data.get("collected_at"),
        data.get("analyzed_at"),
    )
    if any(ts is not None for ts in timestamps):
        details.append("时间戳 存在 -> +4")
        earned += 4
    else:
        details.append("时间戳 全部缺失 -> +0")

    return DimensionScore("格式规范", 20, earned, details)


def _score_tags(data: dict[str, Any]) -> DimensionScore:
    """标签精度评分（满分 15 分）。"""
    details: list[str] = []
    tags = data.get("tags", [])

    if not isinstance(tags, list) or not tags:
        return DimensionScore("标签精度", 15, 0, ["tags 为空或非列表"])

    string_tags = [t for t in tags if isinstance(t, str)]
    if not string_tags:
        return DimensionScore("标签精度", 15, 3, ["tags 中无有效字符串标签"])

    valid_tags = [t for t in string_tags if t in STANDARD_TAGS]
    invalid_tags = [t for t in string_tags if t not in STANDARD_TAGS]

    details.append(
        f"标签总数: {len(string_tags)}，合法: {len(valid_tags)}，"
        f"非标准: {len(invalid_tags)}"
    )

    valid_count = len(valid_tags)

    if 1 <= valid_count <= 3:
        score = 15
        details.append("合法标签数 1-3 个 -> 满分 15")
    elif 4 <= valid_count <= 5:
        score = 10
        details.append("合法标签数 4-5 个 -> 10 分")
    elif valid_count == 0:
        score = 3
        details.append("无合法标签 -> 3 分")
    else:
        score = 6
        details.append(f"合法标签数 {valid_count} 个（过多）-> 6 分")

    if invalid_tags:
        details.append(f"非标准标签: {', '.join(invalid_tags[:5])}")

    return DimensionScore("标签精度", 15, score, details)


def _score_buzzword(data: dict[str, Any]) -> DimensionScore:
    """空洞词检测评分（满分 15 分）。"""
    details: list[str] = []
    fields = [
        str(data.get("summary", "") or ""),
        str(data.get("title", "") or ""),
    ]
    tags = data.get("tags", [])
    if isinstance(tags, list):
        fields.extend(str(t) for t in tags if isinstance(t, str))

    text = " ".join(fields)
    text_lower = text.lower()

    found_cn = [w for w in BUZZWORD_CN if w in text]
    found_en = [w for w in BUZZWORD_EN if w in text_lower]
    all_found = found_cn + found_en

    if not all_found:
        return DimensionScore("空洞词检测", 15, 15, ["未检测到空洞词 -> 满分 15"])

    deduction = min(15, len(all_found) * 3)
    score = max(0, 15 - deduction)
    details.append(f"检测到 {len(all_found)} 个空洞词: {', '.join(all_found)}")
    details.append(f"扣 {deduction} 分 -> {score}/15")

    return DimensionScore("空洞词检测", 15, score, details)


# ---------------------------------------------------------------------------
# 主评分函数
# ---------------------------------------------------------------------------


def score_file(filepath: Path) -> QualityReport:
    """对单个 JSON 文件执行 5 维度质量评分。"""
    errors: list[str] = []

    try:
        text = filepath.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f"JSON 解析失败: {exc}")
        return QualityReport(filepath, 0, "C", [], errors)
    except OSError as exc:
        errors.append(f"读取文件出错: {exc}")
        return QualityReport(filepath, 0, "C", [], errors)

    if not isinstance(data, dict):
        errors.append("根元素非 JSON 对象")
        return QualityReport(filepath, 0, "C", [], errors)

    dimensions: list[DimensionScore] = [
        _score_summary(data),
        _score_depth(data),
        _score_format(data),
        _score_tags(data),
        _score_buzzword(data),
    ]

    total = sum(d.score for d in dimensions)

    if total >= 80:
        grade = "A"
    elif total >= 60:
        grade = "B"
    else:
        grade = "C"

    return QualityReport(filepath, total, grade, dimensions, errors)


# ---------------------------------------------------------------------------
# 文件解析与渲染
# ---------------------------------------------------------------------------


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


def _render_bar(score: int, max_score: int, width: int = 20) -> str:
    """渲染可视化进度条。"""
    if max_score <= 0:
        filled = 0
    else:
        filled = int(score / max_score * width)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    """入口函数。存在 C 级条目返回 1，否则返回 0。"""
    parser = argparse.ArgumentParser(description="知识条目 5 维度质量评分工具")
    parser.add_argument(
        "files",
        nargs="+",
        help="JSON 文件路径，支持通配符（如 'knowledge/articles/*.json'）",
    )
    args = parser.parse_args()

    filepaths = _resolve_files(args.files)

    if not filepaths:
        print("错误：未找到匹配的文件。", file=sys.stderr)
        return 1

    json_files = [fp for fp in filepaths if fp.suffix == ".json"]
    skipped = len(filepaths) - len(json_files)
    total_files = len(json_files)

    if total_files == 0:
        print("错误：没有找到 .json 文件。", file=sys.stderr)
        return 1

    has_c = False
    results: list[QualityReport] = []

    bar_width = 30
    for idx, fp in enumerate(json_files, 1):
        pct = idx * 100 // total_files
        filled = idx * bar_width // total_files
        bar = "#" * filled + "-" * (bar_width - filled)
        print(f"\r[{bar}] {pct:3d}% ({idx}/{total_files}) {fp.name}", end="")
        sys.stdout.flush()

        results.append(score_file(fp))

    print()

    if skipped:
        print(f"\n跳过 {skipped} 个非 JSON 文件。")

    total_a = total_b = total_c = 0

    for report in results:
        print(f"\n{'─' * 60}")
        print(f"文件: {report.filepath.name}")

        if report.errors:
            for err in report.errors:
                print(f"  错误: {err}")
            total_c += 1
            has_c = True
            continue

        for dim in report.dimensions:
            bar = _render_bar(dim.score, dim.max_score, width=20)
            print(f"  {dim.name:<6s} {bar} {dim.score:2d}/{dim.max_score:2d}")
            for detail in dim.details:
                print(f"      {detail}")

        print(f"  {'─' * 40}")
        print(f"  总分: {report.total_score}/100  等级: {report.grade}")

        if report.grade == "A":
            total_a += 1
        elif report.grade == "B":
            total_b += 1
        else:
            total_c += 1
            has_c = True

    print(f"\n{'=' * 60}")
    print(f"汇总: 共 {total_files} 个文件")
    print(f"  A (>= 80): {total_a}")
    print(f"  B (>= 60): {total_b}")
    print(f"  C (< 60):  {total_c}")
    print(f"{'=' * 60}")

    return 1 if has_c else 0


if __name__ == "__main__":
    sys.exit(main())
