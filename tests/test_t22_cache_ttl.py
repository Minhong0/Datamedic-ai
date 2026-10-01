"""T22 테스트: LLM 캐시 TTL 만료 + 히트율 통계."""
from __future__ import annotations

import time
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel

import llm.client as client_mod
from llm.client import complete_json, cache_hit_rate, cache_stats


class Resp(BaseModel):
    explanation: str


class FakeLLM:
    def __init__(self, text: str) -> None:
        self._text = text
        self.call_count = 0

    def complete(self, prompt: str) -> str:
        self.call_count += 1
        return self._text


def test_cache_hit_rate_starts_zero():
    """통계 초기화 없이 hit/miss가 모두 없으면 0.0이다."""
    # 통계를 직접 리셋
    client_mod._cache_stats["hits"] = 0
    client_mod._cache_stats["misses"] = 0
    assert cache_hit_rate() == 0.0


def test_cache_hit_increments_stat(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod.settings, "cache_dir", tmp_path)
    monkeypatch.setattr(client_mod, "_CACHE_TTL_SECONDS", 0)
    fake = FakeLLM('{"explanation": "ok"}')
    monkeypatch.setattr(client_mod, "_provider", fake)
    client_mod._cache_stats["hits"] = 0
    client_mod._cache_stats["misses"] = 0

    complete_json("explain_issue", {"k": "v1"}, Resp)  # miss → LLM 호출
    complete_json("explain_issue", {"k": "v1"}, Resp)  # hit → 캐시

    stats = cache_stats()
    assert stats["hits"] >= 1
    assert stats["misses"] >= 1
    assert fake.call_count == 1


def test_cache_ttl_expiry(tmp_path, monkeypatch):
    """TTL이 0초일 때 캐시 파일이 오래되면 재호출해야 한다."""
    monkeypatch.setattr(client_mod.settings, "cache_dir", tmp_path)
    fake = FakeLLM('{"explanation": "ttl test"}')
    monkeypatch.setattr(client_mod, "_provider", fake)
    # TTL = 1초 설정
    monkeypatch.setattr(client_mod, "_CACHE_TTL_SECONDS", 1)

    complete_json("explain_issue", {"k": "ttl"}, Resp)  # 첫 호출
    assert fake.call_count == 1

    # 캐시 파일 mtime을 2초 전으로 변조
    key = client_mod._cache_key("explain_issue", {"k": "ttl"})
    cache_file = tmp_path / f"{key}.json"
    old_time = time.time() - 2
    import os
    os.utime(cache_file, (old_time, old_time))

    complete_json("explain_issue", {"k": "ttl"}, Resp)  # 만료 → 재호출
    assert fake.call_count == 2


def test_cache_ttl_zero_never_expires(tmp_path, monkeypatch):
    """TTL=0이면 오래된 캐시도 만료되지 않는다."""
    monkeypatch.setattr(client_mod.settings, "cache_dir", tmp_path)
    fake = FakeLLM('{"explanation": "no expire"}')
    monkeypatch.setattr(client_mod, "_provider", fake)
    monkeypatch.setattr(client_mod, "_CACHE_TTL_SECONDS", 0)

    complete_json("explain_issue", {"k": "no_ttl"}, Resp)
    assert fake.call_count == 1

    # mtime을 아주 오래 전으로 설정
    key = client_mod._cache_key("explain_issue", {"k": "no_ttl"})
    cache_file = tmp_path / f"{key}.json"
    import os
    old_time = time.time() - 86400  # 하루 전
    os.utime(cache_file, (old_time, old_time))

    complete_json("explain_issue", {"k": "no_ttl"}, Resp)
    assert fake.call_count == 1  # 재호출 없음
