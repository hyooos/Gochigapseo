"""LLM 호출은 전부 여기서 (에이전트는 openai 직접 안 씀)
실패하면 None → 규칙/문장틀로 대체. temperature 0 + 캐시, 호출마다 비용 기록(runtime/llm_usage.jsonl)
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from ..config import CONFIG_DIR, RUNTIME_DIR, Settings
from ..contracts import LLMUsage

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMRecord:
    purpose: str
    ok: bool
    cached: bool = False
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0.0
    error: str = ""
    elapsed_ms: int = 0
    calls: int = 1


@dataclass
class ToolLoopResult:
    """모델이 고른 function call을 앱에서 실행한 뒤 받은 최종 문장"""
    text: str | None
    record: LLMRecord
    called_tool: str | None = None


@dataclass
class _QuestionBudget:
    calls: int = 0
    records: list[LLMRecord] = field(default_factory=list)


def _load_prices() -> dict:
    p = CONFIG_DIR / "models.yaml"
    if not p.exists():
        return {"default": {"input": 1.0, "output": 4.0}, "models": {}}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def extract_json(text: str) -> Any:
    """응답에서 첫 JSON 객체 꺼냄 (코드블록·앞뒤 설명 있어도 됨)"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON 객체를 찾지 못함")
    return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        runtime_dir: Path = RUNTIME_DIR,
        transport: Callable[[list[dict], str], tuple[str, int, int]] | None = None,
    ):
        """transport: 테스트용 가짜 호출 (messages, model) -> (text, in_tokens, out_tokens)"""
        self.s = settings
        self.runtime_dir = runtime_dir
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.usage_log = runtime_dir / "llm_usage.jsonl"
        self._prices = _load_prices()
        self._transport = transport
        self._client = None
        self._q = _QuestionBudget()
        self._db = None
        if self.s.llm_cache:
            self._db = sqlite3.connect(runtime_dir / "llm_cache.sqlite", check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, ts REAL)")

    # ── 상태 ──
    @property
    def enabled(self) -> bool:
        return self.s.llm_enabled or self._transport is not None

    def new_question(self) -> None:
        """질문 1개 시작할 때 부름 (질문당 호출 상한 초기화)"""
        self._q = _QuestionBudget()

    def question_usage(self) -> LLMUsage:
        u = LLMUsage()
        for r in self._q.records:
            u.calls += 0 if r.cached else r.calls
            u.cached += 1 if r.cached else 0
            u.input_tokens += r.input_tokens
            u.output_tokens += r.output_tokens
            u.est_cost_usd += r.est_cost_usd
        u.est_cost_usd = round(u.est_cost_usd, 6)
        return u

    def question_errors(self) -> list[str]:
        """이번 질문에서 난 LLM 오류 (예: 'plan: schema: goal'). 디버깅용"""
        return [f"{r.purpose}: {r.error}" for r in self._q.records if r.error]

    def month_cost_usd(self) -> float:
        if not self.usage_log.exists():
            return 0.0
        ym = datetime.now().strftime("%Y-%m")
        total = 0.0
        for line in self.usage_log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", "").startswith(ym) and not rec.get("cached"):
                total += float(rec.get("est_cost_usd", 0))
        return round(total, 4)

    # ── 공개 API ──
    def chat_text(self, system: str, user: str, purpose: str) -> tuple[str | None, LLMRecord]:
        return self._call([{"role": "system", "content": system}, {"role": "user", "content": user}], purpose)

    def chat_json(self, system: str, user: str, schema: type[T], purpose: str) -> tuple[T | None, LLMRecord]:
        """JSON 받아서 pydantic으로 검증. 형식 틀리면 None"""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        text, rec = self._call(messages, purpose)
        if text is None:
            return None, rec
        try:
            return schema.model_validate(extract_json(text)), rec
        except ValidationError as ex:  # 어느 필드가 틀렸는지만 남김 (값은 안 남김)
            rec.ok = False
            fields = sorted({".".join(str(x) for x in e["loc"]) or "?" for e in ex.errors()})
            rec.error = "schema: " + ", ".join(fields)
            self._note_error(rec, messages)
            return None, rec
        except ValueError as ex:
            rec.ok = False
            rec.error = f"json: {type(ex).__name__}"
            self._note_error(rec, messages)
            return None, rec

    def call_tool(self, system: str, user: str, tools: list[dict], executor: Callable[[str, dict], dict],
                  purpose: str = "analysis_tool") -> ToolLoopResult:
        """function calling 1회전. executor는 공개해도 되는 집계 결과만 돌려줘야 함
        지원 안 하거나 형식 틀리면 None → 규칙으로
        """
        rec = LLMRecord(purpose=purpose, ok=False, model=self.s.llm_model)
        if not self.enabled or not self.s.llm_tool_calling:
            rec.error = "tool_calling_disabled"
            return ToolLoopResult(None, rec)
        if self._q.calls + 2 > self.s.llm_max_calls_per_question:
            rec.error = "per_question_limit"
            self._log(rec)
            return ToolLoopResult(None, rec)
        if self.month_cost_usd() >= self.s.llm_monthly_budget_usd:
            rec.error = "monthly_budget"
            self._log(rec)
            return ToolLoopResult(None, rec)
        if self._transport is not None:
            # 테스트 transport는 텍스트 응답만 흉내 냄 (tool protocol은 안 됨)
            rec.error = "tool_transport_unavailable"
            return ToolLoopResult(None, rec)

        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        t0 = time.perf_counter()
        try:
            first, tin1, tout1 = self._openai_tool_request(messages, tools)
            self._q.calls += 1
            calls = getattr(first, "tool_calls", None) or []
            if len(calls) != 1:
                raise ValueError("expected_one_tool_call")
            call = calls[0]
            name = str(call.function.name)
            arguments = json.loads(call.function.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool_arguments_not_object")
            output = executor(name, arguments)
            if isinstance(output, dict) and output.get("ok") is False:
                rec.error = output["error"]
                rec.calls = 1
                rec.input_tokens, rec.output_tokens = tin1, tout1
                pin, pout = self._price(self.s.llm_model)
                rec.est_cost_usd = round(tin1 / 1e6 * pin + tout1 / 1e6 * pout, 6)
                rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
                self._log(rec)
                return ToolLoopResult(None, rec)
            assistant_message = {
                "role": "assistant", "content": first.content or "",
                "tool_calls": [{"id": call.id, "type": "function",
                                "function": {"name": name, "arguments": call.function.arguments or "{}"}}],
            }
            messages.extend([assistant_message, {"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(output, ensure_ascii=False)}])
            text, tin2, tout2 = self._openai_call(messages, self.s.llm_model)
            self._q.calls += 1
        except Exception as ex:
            rec.error = f"{type(ex).__name__}"
            rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self._log(rec)
            return ToolLoopResult(None, rec)

        pin, pout = self._price(self.s.llm_model)
        rec.ok, rec.calls, rec.input_tokens, rec.output_tokens = True, 2, tin1 + tin2, tout1 + tout2
        rec.est_cost_usd = round(rec.input_tokens / 1e6 * pin + rec.output_tokens / 1e6 * pout, 6)
        rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self._log(rec)
        return ToolLoopResult(text, rec, name)

    def tool_step(self, messages: list[dict], tools: list[dict], purpose: str) -> tuple[dict | None, LLMRecord]:
        """하위 에이전트 판단 루프 한 걸음: tool 하나 이상 고른 assistant 메시지 돌려줌
        호출 상한·월 예산·캐시·비용 기록은 다른 호출이랑 똑같이. 실패하면 None → 규칙으로
        """
        rec = LLMRecord(purpose=purpose, ok=False, model=self.s.llm_model)
        if not self.enabled:
            rec.error = "offline"
            return None, rec
        if self._transport is not None and "_openai_tool_request" not in self.__dict__:
            rec.error = "tool_transport_unavailable"  # 텍스트만 흉내 내는 테스트 transport: 호출 수 안 셈
            return None, rec
        key = self._cache_key(messages + [{"tools": tools}])
        if self._db is not None:
            row = self._db.execute("SELECT v FROM cache WHERE k=?", (key,)).fetchone()
            if row:
                rec.ok, rec.cached = True, True
                self._log(rec)
                return json.loads(row[0]), rec
        if self._q.calls >= self.s.llm_max_calls_per_question:
            rec.error = "per_question_limit"
            self._log(rec)
            return None, rec
        if self.month_cost_usd() >= self.s.llm_monthly_budget_usd:
            rec.error = "monthly_budget"
            self._log(rec)
            return None, rec
        self._q.calls += 1
        t0 = time.perf_counter()
        try:
            msg, tin, tout = self._openai_tool_request(messages, tools)
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                raise ValueError("no_tool_call")
            out = {"role": "assistant", "content": getattr(msg, "content", None) or "",
                   "tool_calls": [{"id": c.id, "type": "function",
                                   "function": {"name": str(c.function.name), "arguments": c.function.arguments or "{}"}}
                                  for c in calls]}
        except Exception as ex:
            rec.error = f"{type(ex).__name__}"
            rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self._log(rec)
            return None, rec
        pin, pout = self._price(self.s.llm_model)
        rec.ok, rec.input_tokens, rec.output_tokens = True, tin, tout
        rec.est_cost_usd = round(tin / 1e6 * pin + tout / 1e6 * pout, 6)
        rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if self._db is not None:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(out, ensure_ascii=False), time.time()))
            self._db.commit()
        self._log(rec)
        return out, rec

    def transcribe(self, audio: bytes, filename: str = "voice.wav") -> tuple[str | None, LLMRecord]:
        """음성 → 글자 (STT_MODE=api). 음성이 외부로 감, 저장 안 함. 실패하면 None"""
        rec = LLMRecord(purpose="stt", ok=False, model=self.s.stt_model)
        if not self.s.stt_enabled:
            rec.error = "stt_off"
            return None, rec
        t0 = time.perf_counter()
        try:
            if self._client is None:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.s.llm_api_key, base_url=self.s.llm_base_url, timeout=30, max_retries=1)
            resp = self._client.audio.transcriptions.create(model=self.s.stt_model, file=(filename, audio))
            text = (getattr(resp, "text", "") or "").strip()
        except Exception as ex:
            rec.error = type(ex).__name__
            rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self._log(rec)
            return None, rec
        rec.ok = bool(text)
        rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self._log(rec)   # 단가는 config/models.yaml, 없는 모델은 default 단가
        return text or None, rec

    def clear_cache(self) -> None:
        """응답 캐시 비움 ('내 자료 지우기' 누를 때)"""
        if self._db is not None:
            self._db.execute("DELETE FROM cache")
            self._db.commit()

    # ── 내부 ──
    def _note_error(self, rec: LLMRecord, messages: list[dict]) -> None:
        """호출은 됐는데 응답 형식이 틀린 경우: 오류 기록하고 캐시에서 지움
        (안 지우면 같은 질문이 계속 규칙으로만 처리됨)
        """
        if self._db is not None:
            self._db.execute("DELETE FROM cache WHERE k=?", (self._cache_key(messages),))
            self._db.commit()
        line = {"ts": datetime.now().isoformat(timespec="seconds"), "purpose": rec.purpose, "error": rec.error,
                "model": rec.model, "cached": rec.cached, "est_cost_usd": 0}
        with self.usage_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _price(self, model: str) -> tuple[float, float]:
        p = (self._prices.get("models") or {}).get(model) or self._prices.get("default") or {}
        return float(p.get("input", 1.0)), float(p.get("output", 4.0))

    def _cache_key(self, messages: list[dict]) -> str:
        raw = json.dumps({"m": self.s.llm_model, "b": self.s.llm_base_url, "msg": messages}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _log(self, rec: LLMRecord) -> None:
        self._q.records.append(rec)
        line = {"ts": datetime.now().isoformat(timespec="seconds"), **rec.__dict__}
        with self.usage_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _call(self, messages: list[dict], purpose: str) -> tuple[str | None, LLMRecord]:
        rec = LLMRecord(purpose=purpose, ok=False, model=self.s.llm_model)
        if not self.enabled:
            rec.error = "offline"
            return None, rec  # 오프라인은 로그도 안 남김

        key = self._cache_key(messages)
        if self._db is not None:
            row = self._db.execute("SELECT v FROM cache WHERE k=?", (key,)).fetchone()
            if row:
                rec.ok, rec.cached = True, True
                self._log(rec)
                return row[0], rec

        if self._q.calls >= self.s.llm_max_calls_per_question:
            rec.error = "per_question_limit"
            self._log(rec)
            return None, rec
        if self.month_cost_usd() >= self.s.llm_monthly_budget_usd:
            rec.error = "monthly_budget"
            self._log(rec)
            return None, rec

        self._q.calls += 1
        t0 = time.perf_counter()
        try:
            text, tin, tout = (self._transport or self._openai_call)(messages, self.s.llm_model)
        except Exception as ex:  # 네트워크·인증·모델명 오류 전부 규칙으로
            rec.error = f"{type(ex).__name__}"
            rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self._log(rec)
            return None, rec

        pin, pout = self._price(self.s.llm_model)
        rec.ok, rec.input_tokens, rec.output_tokens = True, tin, tout
        rec.est_cost_usd = round(tin / 1e6 * pin + tout / 1e6 * pout, 6)
        rec.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if self._db is not None:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, text, time.time()))
            self._db.commit()
        self._log(rec)
        return text, rec

    def _create(self, **kw):
        """chat.completions 호출. temperature 0 + 생각 깊이(LLM_REASONING_EFFORT) 붙임
        모델이 거부하는 값은 그것만 빼고 한 번 더 보냄 (추론형은 temperature, 일반 모델은 reasoning을 거부할 수 있음)
        """
        extra: dict = {"temperature": 0}
        effort = getattr(self.s, "llm_reasoning_effort", "")
        if effort:
            if "openrouter" in self.s.llm_base_url:
                extra["extra_body"] = {"reasoning": {"effort": effort}}
            else:
                extra["reasoning_effort"] = effort
        for _ in range(3):
            try:
                return self._client.chat.completions.create(**kw, **extra)
            except Exception as ex:
                msg = str(ex).lower()
                drop = [k for k, words in (("temperature", ("temperature",)),
                                           ("reasoning_effort", ("reasoning",)), ("extra_body", ("reasoning",)))
                        if k in extra and any(w in msg for w in words)]
                if not drop:
                    raise
                for k in drop:
                    extra.pop(k)
        return self._client.chat.completions.create(**kw)

    def _openai_call(self, messages: list[dict], model: str) -> tuple[str, int, int]:
        if self._client is None:
            from openai import OpenAI  # 오프라인이면 import도 안 함

            self._client = OpenAI(api_key=self.s.llm_api_key, base_url=self.s.llm_base_url, timeout=30, max_retries=1)
        resp = self._create(model=model, messages=messages)
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return text, int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)

    def _openai_tool_request(self, messages: list[dict], tools: list[dict]):
        """첫 요청은 function tool 하나를 꼭 고르게 함"""
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.s.llm_api_key, base_url=self.s.llm_base_url, timeout=30, max_retries=1)
        resp = self._create(model=self.s.llm_model, messages=messages, tools=tools, tool_choice="required")
        usage = getattr(resp, "usage", None)
        return (resp.choices[0].message, int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0))
