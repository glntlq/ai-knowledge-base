"""Shared paths and workflow constants for LangGraph nodes."""

from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ARTICLES_DIR = ROOT_DIR / "knowledge" / "articles"
GITHUB_TRENDING_URL = "https://github.com/trending"
MAX_REVIEW_ITERATIONS = 3
