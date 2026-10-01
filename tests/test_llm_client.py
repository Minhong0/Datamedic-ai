"""T09: FakeLLM으로 llm/client.py 테스트."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel

import llm.client as client_mod
from llm.client import complete_json, mask_sensitive, set_provider


# ── FakeLLM ───────────────────────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, prompt: str) -> str:
        return self._response


# ── 응답 스키마 픽스처 ────────────────────────────────────────────────────────

class ExplainResponse(BaseModel):
    explanation: str


class ColumnSemantics(BaseModel):
    columns: list[dict]


# ── 테스트 ────────────────────────────────────────────────────────────────────

def setup_function():
    set_provider(None)  # type: ignore


def test_complete_json_basic(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "_provider", FakeLLM('{"explanation": "날짜 형식 오류입니다."}'))
    result = complete_json("explain_issue", {"rule_id": "DATE_FORMAT", "value": "20260101", "message": "오류"}, ExplainResponse, use_cache=False)
    assert isinstance(result, ExplainResponse)
    assert "날짜" in result.explanation


def test_complete_json_from_code_block(monkeypatch):
    raw = '```json\n{"explanation": "테스트"}\n```'
    monkeypatch.setattr(client_mod, "_provider", FakeLLM(raw))
    result = complete_json("explain_issue", {}, ExplainResponse, use_cache=False)
    assert result.explanation == "테스트"


def test_complete_json_retries_then_raises(monkeypatch):
    class FailLLM:
        def complete(self, prompt: str) -> str:
            return "not-json-at-all {{{invalid"
    monkeypatch.setattr(client_mod, "_provider", FailLLM())
    with pytest.raises(RuntimeError):
        complete_json("explain_issue", {}, ExplainResponse, use_cache=False)


def test_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod.settings, "cache_dir", tmp_path)
    call_count = [0]

    class CountingLLM:
        def complete(self, prompt: str) -> str:
            call_count[0] += 1
            return '{"explanation": "캐시 테스트"}'

    monkeypatch.setattr(client_mod, "_provider", CountingLLM())
    r1 = complete_json("explain_issue", {"x": "1"}, ExplainResponse)
    r2 = complete_json("explain_issue", {"x": "1"}, ExplainResponse)
    assert r1.explanation == r2.explanation
    assert call_count[0] == 1  # 두 번째는 캐시에서


def test_mask_sensitive_bizno():
    assert mask_sensitive("123-45-67819") == "123-45-*****"


def test_mask_sensitive_phone():
    result = mask_sensitive("010-1234-5678")
    assert "****" in result
    assert "5678" not in result


def test_mask_sensitive_email():
    result = mask_sensitive("hello@example.com")
    assert "@example.com" in result
    assert "hello" not in result or result.startswith("he")


def test_prompt_file_missing(monkeypatch):
    monkeypatch.setattr(client_mod, "_provider", FakeLLM("{}"))
    with pytest.raises(FileNotFoundError):
        complete_json("nonexistent_prompt", {}, ExplainResponse, use_cache=False)
