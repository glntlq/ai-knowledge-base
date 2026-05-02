"""Unified LLM model client for OpenAI-compatible providers.

This module provides a single interface to call multiple LLM providers that
support the OpenAI-compatible REST API shape (e.g. /v1/chat/completions).

Supported providers:
- DeepSeek
- Qwen (DashScope compatible-mode)
- OpenAI

Provider selection is controlled via environment variables:
- LLM_PROVIDER: deepseek|qwen|openai (default: deepseek)
- <PROVIDER>_API_KEY: DEEPSEEK_API_KEY / QWEN_API_KEY / OPENAI_API_KEY
- Optional base URL overrides:
  - DEEPSEEK_BASE_URL
  - QWEN_BASE_URL
  - OPENAI_BASE_URL

This module intentionally does NOT depend on the OpenAI SDK; it uses httpx
directly.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

import httpx

logger = logging.getLogger(__name__)


JsonDict = Dict[str, Any]
ChatMessage = Mapping[str, Any]


class LLMError(RuntimeError):
    """Raised when an LLM call fails."""


@dataclass(frozen=True)
class Usage:
    """Token usage returned by the provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    """Normalized LLM response."""

    content: str
    usage: Usage
    provider: str
    model: str
    raw: JsonDict


@dataclass(frozen=True)
class PriceUSDPer1M:
    """Pricing in USD per 1M tokens."""

    input: Decimal
    output: Decimal


class LLMProvider(ABC):
    """Abstract LLM provider interface."""

    @abstractmethod
    def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> LLMResponse:
        """Run a chat completion request."""


class OpenAICompatibleProvider(LLMProvider):
    """An OpenAI-compatible REST provider implemented with httpx."""

    def __init__(
        self,
        *,
        provider_name: str,
        api_key: str,
        base_url: str,
        timeout_s: float = 60.0,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not base_url:
            raise ValueError("base_url is required")

        self._provider_name = provider_name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s)
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if default_headers:
            self._headers.update(dict(default_headers))

        self._client = httpx.Client(
            timeout=self._timeout,
            headers=self._headers,
        )

    @property
    def provider_name(self) -> str:
        """Provider name."""

        return self._provider_name

    def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": list(messages),
        }  # type: JsonDict
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(dict(extra))

        url = f"{self._base_url}/chat/completions"

        try:
            resp = self._client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(f"{self._provider_name} request timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self._provider_name} request failed: {exc}") from exc

        if resp.status_code >= 400:
            detail = _safe_json(resp)
            raise LLMError(
                f"{self._provider_name} HTTP {resp.status_code}: {detail}"
            )

        data = resp.json()

        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        if not isinstance(content, str):
            content = str(content)

        usage_obj = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_obj.get("prompt_tokens") or 0),
            completion_tokens=int(usage_obj.get("completion_tokens") or 0),
            total_tokens=int(usage_obj.get("total_tokens") or 0),
        )

        return LLMResponse(
            content=content,
            usage=usage,
            provider=self._provider_name,
            model=model,
            raw=data,
        )


def _safe_json(resp: httpx.Response) -> str:
    """Safely extract a short JSON/text detail from an HTTP response."""

    try:
        payload = resp.json()
        return json.dumps(payload, ensure_ascii=False)[:2000]
    except Exception:  # noqa: BLE001 - best effort for diagnostics
        return (resp.text or "").strip()[:2000]


def get_provider_from_env() -> LLMProvider:
    """Create a provider instance from environment variables."""

    provider = (os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").strip()
        return OpenAICompatibleProvider(
            provider_name="deepseek",
            api_key=api_key,
            base_url=base_url,
        )

    if provider == "qwen":
        api_key = os.getenv("QWEN_API_KEY", "").strip()
        base_url = (
            os.getenv("QWEN_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).strip()
        return OpenAICompatibleProvider(
            provider_name="qwen",
            api_key=api_key,
            base_url=base_url,
        )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
        return OpenAICompatibleProvider(
            provider_name="openai",
            api_key=api_key,
            base_url=base_url,
        )

    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")


def create_provider() -> LLMProvider:
    """Create a provider from environment variables.

    This is an alias kept for callers that prefer a verb-based factory name.
    """

    return get_provider_from_env()


def chat_with_retry(
    *,
    provider: Optional[LLMProvider] = None,
    messages: Sequence[ChatMessage],
    model: str,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    extra: Optional[Mapping[str, Any]] = None,
    retries: int = 3,
    base_backoff_s: float = 1.0,
) -> LLMResponse:
    """Chat with retry (exponential backoff).

    Retries up to `retries` times (default 3). Backoff is exponential with jitter:
    base * 2^attempt + random[0, 0.25].
    """

    p = provider or get_provider_from_env()

    last_exc = None  # type: Optional[Exception]
    for attempt in range(retries):
        try:
            return p.chat(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                extra=extra,
            )
        except (LLMError, httpx.HTTPError, httpx.TimeoutException) as exc:
            last_exc = exc
            is_last = attempt >= retries - 1
            logger.warning(
                "LLM call failed (attempt %d/%d): %s",
                attempt + 1,
                retries,
                exc,
            )
            if is_last:
                break
            backoff = (base_backoff_s * (2**attempt)) + random.random() * 0.25
            time.sleep(backoff)

    raise LLMError("LLM call failed after retries") from last_exc


def quick_chat(
    prompt: str,
    *,
    model: str,
    provider: Optional[LLMProvider] = None,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> LLMResponse:
    """Convenience function: one-line chat call."""

    messages = []  # type: List[Dict[str, str]]
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    return chat_with_retry(
        provider=provider,
        messages=messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def estimate_tokens_text(text: str) -> int:
    """Estimate token count for plain text.

    This is a heuristic estimate intended for cost previews when providers do not
    return usage, or for offline estimation. It is not exact.
    """

    if not text:
        return 0
    # Rough heuristic: ~4 chars/token for English-like text. For CJK, char/token
    # is closer to 1-2. We blend by weighting non-ASCII as "denser".
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    approx = (ascii_chars / 4.0) + (non_ascii / 1.6)
    return max(1, int(approx))


def estimate_tokens_messages(messages: Sequence[ChatMessage]) -> int:
    """Estimate token count for a list of chat messages."""

    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens_text(content)
        else:
            total += estimate_tokens_text(str(content))
        role = m.get("role", "")
        total += 2 if role else 0
    # A small overhead for chat formatting.
    return total + 3


def load_pricing_from_env() -> Dict[str, PriceUSDPer1M]:
    """Load pricing table from env.

    Env format:
      LLM_PRICING_JSON='{"gpt-4o-mini":{"input":0.15,"output":0.60}}'

    Values are USD per 1M tokens. This is recommended because pricing changes
    frequently.
    """

    raw = (os.getenv("LLM_PRICING_JSON") or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("LLM_PRICING_JSON is not valid JSON") from exc

    out = {}  # type: Dict[str, PriceUSDPer1M]
    if not isinstance(parsed, dict):
        raise ValueError("LLM_PRICING_JSON must be a JSON object")

    for model, v in parsed.items():
        if not isinstance(model, str) or not isinstance(v, dict):
            continue
        if "input" not in v or "output" not in v:
            continue
        out[model] = PriceUSDPer1M(
            input=Decimal(str(v["input"])),
            output=Decimal(str(v["output"])),
        )

    return out


def get_default_pricing() -> Dict[str, PriceUSDPer1M]:
    """Return a small default pricing table.

    These defaults are intentionally minimal; prefer setting LLM_PRICING_JSON to
    keep pricing accurate.
    """

    return {
        # Common placeholders; override via LLM_PRICING_JSON for accuracy.
        "gpt-4o-mini": PriceUSDPer1M(input=Decimal("0.15"), output=Decimal("0.60")),
        "gpt-4.1-mini": PriceUSDPer1M(input=Decimal("0.30"), output=Decimal("1.20")),
        "deepseek-chat": PriceUSDPer1M(input=Decimal("0.20"), output=Decimal("0.80")),
        "qwen-turbo": PriceUSDPer1M(input=Decimal("0.20"), output=Decimal("0.80")),
    }


def compute_cost_usd(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    pricing: Optional[Mapping[str, PriceUSDPer1M]] = None,
) -> Optional[Decimal]:
    """Compute USD cost for a request based on token usage.

    Returns None if pricing for the model is unavailable.
    """

    table = dict(get_default_pricing())
    table.update(load_pricing_from_env())
    if pricing:
        table.update(dict(pricing))

    price = table.get(model)
    if not price:
        return None

    pt = Decimal(prompt_tokens)
    ct = Decimal(completion_tokens)
    return (pt * price.input + ct * price.output) / Decimal(1_000_000)


def compute_cost_from_response(
    resp: LLMResponse,
    *,
    pricing: Optional[Mapping[str, PriceUSDPer1M]] = None,
) -> Optional[Decimal]:
    """Compute USD cost using response usage."""

    return compute_cost_usd(
        model=resp.model,
        prompt_tokens=resp.usage.prompt_tokens,
        completion_tokens=resp.usage.completion_tokens,
        pricing=pricing,
    )


def _env_or_raise(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value


def _example_env(provider: str) -> List[str]:
    if provider == "deepseek":
        return ["export LLM_PROVIDER=deepseek", "export DEEPSEEK_API_KEY=..."]
    if provider == "qwen":
        return ["export LLM_PROVIDER=qwen", "export QWEN_API_KEY=..."]
    return ["export LLM_PROVIDER=openai", "export OPENAI_API_KEY=..."]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    provider_name = (os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()
    try:
        if provider_name == "deepseek":
            _env_or_raise("DEEPSEEK_API_KEY")
            default_model = os.getenv("LLM_MODEL") or "deepseek-chat"
        elif provider_name == "qwen":
            _env_or_raise("QWEN_API_KEY")
            default_model = os.getenv("LLM_MODEL") or "qwen-turbo"
        else:
            _env_or_raise("OPENAI_API_KEY")
            default_model = os.getenv("LLM_MODEL") or "gpt-4o-mini"

        logger.info("提供商: %s", provider_name)
        logger.info(
            "创建 LLM 客户端: provider=%s, model=%s",
            provider_name,
            default_model,
        )

        resp = quick_chat(
            "用一句话解释什么是 RAG，并给一个工程落地场景。",
            model=default_model,
            temperature=0.2,
        )

        cost = compute_cost_from_response(resp)
        if cost is not None:
            cost_display = cost.quantize(Decimal("0.000001"))
            logger.info(
                "Token 用量: %d (prompt) + %d (completion) = %d, 估算成本: $%s",
                resp.usage.prompt_tokens,
                resp.usage.completion_tokens,
                resp.usage.total_tokens,
                format(cost_display, "f"),
            )
        else:
            logger.info(
                "Token 用量: %d (prompt) + %d (completion) = %d, 估算成本: $N/A",
                resp.usage.prompt_tokens,
                resp.usage.completion_tokens,
                resp.usage.total_tokens,
            )

        logger.info("Content:\n%s", resp.content)
    except Exception as exc:  # noqa: BLE001 - CLI entry
        logger.error("Test run failed: %s", exc)
        logger.info("Example env:\n%s", "\n".join(_example_env(provider_name)))
        raise

