"""Workflow-level LLM helper functions.

This module exposes a compact interface for LangGraph nodes while reusing the
project's existing provider selection, retry, and pricing logic.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared, fallback helps bare test envs
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env", override=False)


def _default_model() -> str:
    provider = (os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()
    if provider == "qwen":
        return os.getenv("LLM_MODEL") or "qwen-turbo"
    if provider == "openai":
        return os.getenv("LLM_MODEL") or "gpt-4o-mini"
    return os.getenv("LLM_MODEL") or "deepseek-chat"


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if isinstance(usage, Mapping):
        return dict(usage)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _strip_json_fence(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def chat(
    prompt: str,
    system: str = "",
    *,
    temperature: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call the configured LLM and return `(text, usage)`."""

    from pipeline.model_client import compute_cost_from_response, quick_chat

    temp = 0.2 if temperature is None else temperature
    resp = quick_chat(
        prompt,
        model=_default_model(),
        system=system or None,
        temperature=temp,
    )
    usage = _usage_to_dict(resp.usage)
    usage["provider"] = resp.provider
    usage["model"] = resp.model

    cost = compute_cost_from_response(resp)
    if cost is not None:
        usage["cost_usd"] = float(cost)

    return resp.content, usage


def chat_json(
    prompt: str,
    system: str = "",
    *,
    temperature: float | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Call the LLM and parse the response as JSON."""

    text, usage = chat(prompt, system=system, temperature=temperature)
    return json.loads(_strip_json_fence(text)), usage


def accumulate_usage(
    tracker: dict[str, Any],
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    """Accumulate token and cost usage into `tracker` and return it."""

    tracker["prompt_tokens"] = int(tracker.get("prompt_tokens") or 0) + int(
        usage.get("prompt_tokens") or 0
    )
    tracker["completion_tokens"] = int(tracker.get("completion_tokens") or 0) + int(
        usage.get("completion_tokens") or 0
    )
    tracker["total_tokens"] = int(tracker.get("total_tokens") or 0) + int(
        usage.get("total_tokens") or 0
    )

    if usage.get("cost_usd") is not None:
        current_cost = Decimal(str(tracker.get("cost_usd") or "0"))
        tracker["cost_usd"] = float(current_cost + Decimal(str(usage["cost_usd"])))

    model = str(usage.get("model") or "").strip()
    provider = str(usage.get("provider") or "").strip()
    if model:
        tracker["model"] = model
    if provider:
        tracker["provider"] = provider

    return tracker
