from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel

from config import settings

T = TypeVar("T", bound=BaseModel)

_PROMPTS_DIR = Path(__file__).parent / "prompts"
logger = logging.getLogger(__name__)

_cache_stats: dict[str, int] = {"hits": 0, "misses": 0}

_CACHE_TTL_SECONDS: int = int(getattr(settings, "llm_cache_ttl_seconds", 0) or 0)


class LLMProvider(Protocol):
    def complete(self, prompt: str) -> str: ...


def _load_prompt(name: str, variables: dict) -> str:
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"프롬프트 파일 없음: {path}")
    template = path.read_text(encoding="utf-8")
    for k, v in variables.items():
        template = template.replace(f"{{{{ {k} }}}}", str(v))
    return template


def _cache_key(prompt_name: str, variables: dict) -> str:
    raw = json.dumps({"prompt": prompt_name, "vars": variables}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    path = settings.cache_dir / f"{key}.json"
    if not path.exists():
        _cache_stats["misses"] += 1
        return None
    if _CACHE_TTL_SECONDS > 0:
        age = time.time() - path.stat().st_mtime
        if age > _CACHE_TTL_SECONDS:
            path.unlink(missing_ok=True)
            _cache_stats["misses"] += 1
            logger.debug("캐시 만료: %s (%.0f초 경과)", key, age)
            return None
    _cache_stats["hits"] += 1
    return path.read_text(encoding="utf-8")


def _cache_set(key: str, value: str) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    (settings.cache_dir / f"{key}.json").write_text(value, encoding="utf-8")


def cache_hit_rate() -> float:
    """현재 세션의 캐시 히트율(0.0~1.0)을 반환한다."""
    total = _cache_stats["hits"] + _cache_stats["misses"]
    return _cache_stats["hits"] / total if total > 0 else 0.0


def cache_stats() -> dict[str, int]:
    """현재 세션의 캐시 통계를 반환한다."""
    return dict(_cache_stats)


def _extract_json(text: str) -> str:
    m = re.search(r"```json\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip()
    for start, end in [(text.find("{"), "}"), (text.find("["), "]")]:
        if start >= 0:
            depth, i = 0, start
            open_ch = text[start]
            close_ch = "}" if open_ch == "{" else "]"
            for i in range(start, len(text)):
                if text[i] == open_ch:
                    depth += 1
                elif text[i] == close_ch:
                    depth -= 1
                    if depth == 0:
                        return text[start: i + 1]
    return text.strip()


class _AnthropicProvider:
    def complete(self, prompt: str) -> str:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.llm_model_claude,
            max_tokens=1024,
            temperature=settings.llm_temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text


class _OpenAIProvider:
    def complete(self, prompt: str) -> str:
        import openai
        client = openai.OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model=settings.llm_model_openai,
            temperature=settings.llm_temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""


def _default_provider() -> LLMProvider:
    if settings.llm_provider == "openai":
        return _OpenAIProvider()
    return _AnthropicProvider()


_provider: LLMProvider | None = None


def set_provider(p: LLMProvider) -> None:
    global _provider
    _provider = p


def complete_json(
    prompt_name: str,
    variables: dict,
    schema: type[T],
    *,
    use_cache: bool = True,
) -> T:
    key = _cache_key(prompt_name, variables)

    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            try:
                result = schema.model_validate_json(cached)
                logger.debug("캐시 히트: %s (히트율 %.0f%%)", prompt_name, cache_hit_rate() * 100)
                return result
            except Exception:
                pass

    prompt = _load_prompt(prompt_name, variables)
    provider = _provider or _default_provider()

    last_exc: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            raw = provider.complete(prompt)
            json_str = _extract_json(raw)
            result = schema.model_validate_json(json_str)
            if use_cache:
                _cache_set(key, json_str)
            logger.debug("LLM 호출: %s (히트율 %.0f%%)", prompt_name, cache_hit_rate() * 100)
            return result
        except Exception as e:
            last_exc = e

    raise RuntimeError(
        f"LLM 호출 실패 ({prompt_name}, {settings.llm_max_retries + 1}회 시도): {last_exc}"
    )


import re as _re

_BIZNO_RE = _re.compile(r"\d{3}-\d{2}-(\d{5})")
_PHONE_RE = _re.compile(r"(0\d{1,2}-\d{3,4}-)(\d{4})")
_EMAIL_RE = _re.compile(r"([\w.+-]+)@([\w.-]+)")


def mask_sensitive(value: str) -> str:
    v = _BIZNO_RE.sub(lambda m: m.group(0).replace(m.group(1), "*****"), value)
    v = _PHONE_RE.sub(lambda m: m.group(1) + "****", v)
    v = _EMAIL_RE.sub(lambda m: m.group(1)[:2] + "***@" + m.group(2), v)
    return v
