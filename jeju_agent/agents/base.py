"""하위 에이전트 공통 틀

- 숫자는 전부 코드로 계산. LLM은 tools 중에 뭘 어떤 조건으로 할지만 고름 (울타리 벗어나면 막음)
- 키 없거나 실패하면 _run() 규칙 흐름. 어느 쪽이든 self.step()으로 기록
- 원본 행이랑 이용자 조건은 trace·로그에 넣으면 안 됨
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import PROMPT_DIR, Settings
from ..contracts import AgentResult, AgentWarning, AnalysisRequest, TraceStep
from .. import progress
from ..privacy import scrub
from ..data import DataNotReady, Repository


@dataclass
class AgentContext:
    repo: Repository
    settings: Settings
    memory: dict = field(default_factory=dict)            # 세션 기억 (직전 결과 등)
    call: Callable[[str, AnalysisRequest], AgentResult] | None = None  # 다른 에이전트 호출
    llm: Any = None                                       # LLMClient. 없거나 꺼져 있으면 규칙 판단

    def ask(self, agent: str, req: AnalysisRequest) -> AgentResult:
        if self.call is None:
            raise RuntimeError("에이전트 간 호출은 오케스트레이터를 통해서만 가능합니다.")
        return self.call(agent, req)


@dataclass
class AgentTool:
    """LLM이 고를 수 있는 도구 하나. 계산은 run()이 하고 LLM은 인자만 정함
    check: 막을 이유(문장) 돌려주면 실행 안 함 / observe: LLM한테 돌려줄 요약
    """
    name: str
    description: str
    params: dict[str, dict]
    run: Callable[[dict], AgentResult]
    check: Callable[[dict], str | None] | None = None
    observe: Callable[[AgentResult], dict] | None = None


FINISH = "finish"


def _validate(params: dict[str, dict], args: dict) -> str | None:
    """공통 울타리: 모르는 조건, enum 밖 값, minimum·maximum 밖 값 막음"""
    for k, v in args.items():
        spec = params.get(k)
        if spec is None:
            return f"'{k}'는 이 도구가 받지 않는 조건입니다. 받는 조건: {', '.join(params) or '없음'}"
        if v is None:
            continue
        if "enum" in spec and v not in spec["enum"]:
            return f"{k}={v!r}는 허용되지 않습니다. 허용: {spec['enum']}"
        if spec.get("type") == "integer":
            if not isinstance(v, int) or isinstance(v, bool):
                return f"{k}는 정수여야 합니다."
            if "minimum" in spec and v < spec["minimum"] or "maximum" in spec and v > spec["maximum"]:
                return f"{k}={v}는 범위({spec.get('minimum')}~{spec.get('maximum')}) 밖입니다."
        if spec.get("type") == "boolean" and not isinstance(v, bool):
            return f"{k}는 true/false여야 합니다."
    return None


def default_observe(r: AgentResult) -> dict:
    return {"status": r.status, "confidence": r.confidence, "summary": r.summary,
            "findings": [f.text for f in r.findings],
            "warnings": [f"{w.code}: {w.message}" for w in r.warnings],
            "evidence": [f"{e.id} {e.title} ({len(e.rows)}행)" for e in r.evidence]}


class BaseAgent(ABC):
    name: str = "base"
    title: str = ""            # 화면 표시용 이름
    description: str = ""      # 오케스트레이터(LLM 계획)가 읽는 역할 설명
    required: tuple[str, ...] = ()   # finish 전에 꼭 한 번은 실행해야 하는 도구
    reportable: tuple[str, ...] = ()   # finish로 고를 수 있는 결과를 낸 도구. 비우면 전부
    extra_steps: int = 0               # 도구 많은 에이전트는 기본 단계 수에 더함

    def __init__(self) -> None:
        self._trace: list[TraceStep] = []
        self._t0 = 0.0
        self._profile = None

    def step(self, action: str, detail: str = "") -> None:
        """기록 한 줄. LLM 판단 이유에 이용자 조건 섞여도 기록엔 안 남김 (privacy.scrub)"""
        self._trace.append(TraceStep(agent=self.name, action=action, detail=scrub(detail, self._profile),
                                     elapsed_ms=int((time.perf_counter() - self._t0) * 1000)))
        progress.emit(self.name, action, self._trace[-1].detail)   # 화면 '분석 과정' 알림 (조건 낱말은 이미 가려짐)

    def run(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult:
        self._trace, self._t0, self._profile = [], time.perf_counter(), req.profile
        self.step("start", f"intent={req.intent.value} region={req.region or '전체'}")
        try:
            result = self._run_llm(req, ctx) if self._llm_on(req, ctx) else None
            if result is None:
                result = self._run(req, ctx)
        except DataNotReady as ex:
            result = AgentResult(agent=self.name, status="insufficient", summary=str(ex), confidence="low",
                                 warnings=[AgentWarning(code="DATA_NOT_READY", message=str(ex), severity="block")])
        except Exception as ex:  # 에이전트 하나 실패해도 전체는 안 멈춤
            result = AgentResult(agent=self.name, status="error", confidence="low",
                                 summary=f"{self.title or self.name} 분석 중 오류가 발생했습니다.",
                                 warnings=[AgentWarning(code="AGENT_ERROR", message=type(ex).__name__, severity="block")])
        self.step("end", f"status={result.status}")
        result.trace = self._trace + result.trace
        if not result.data_labels:
            result.data_labels = [ctx.repo.label]  # type: ignore[list-item]
        return result

    @abstractmethod
    def _run(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult:
        """규칙 판단 흐름. opts(LLM이 고른 값) 있으면 그 값, 없으면 기본값으로 계산"""

    # ── LLM 판단 ──
    def tools(self, req: AnalysisRequest, ctx: AgentContext) -> list[AgentTool]:
        """LLM이 고를 수 있는 도구. 비어 있으면 규칙 판단만"""
        return []

    def user_fixed(self, req: AnalysisRequest) -> dict:
        """이용자가 정한 조건. LLM은 못 바꿈 (인자에 아예 없거나 check가 막음)"""
        fixed = {"region": req.region, "days": req.days, "month": req.month,
                 "profile": ", ".join(req.profile.describe()) or None,
                 "place_ids": len(req.place_ids) or None}
        return {k: v for k, v in fixed.items() if v is not None}

    def _llm_on(self, req: AnalysisRequest, ctx: AgentContext) -> bool:
        return (ctx.llm is not None and ctx.llm.enabled and ctx.settings.agent_llm
                and not req.params.get("called_by") and not req.params.get("opts"))

    def _run_llm(self, req: AnalysisRequest, ctx: AgentContext) -> AgentResult | None:
        """도구 고르기 → 울타리 검사 → 실행 → 결과 보기 반복, finish로 결과 고름
        LLM 실패하거나 필요한 단계 다 못 하면 None → run()이 규칙으로 다시 계산
        """
        tools = {t.name: t for t in self.tools(req, ctx)}
        if not tools:
            return None
        max_steps = ctx.settings.agent_max_steps + self.extra_steps
        schema = [{"type": "function", "function": {
            "name": t.name, "description": t.description,
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {**t.params, "reason": {"type": "string", "description": "이 도구·조건을 고른 이유 한 문장"}},
                           "required": ["reason"]}}} for t in tools.values()]
        schema.append({"type": "function", "function": {
            "name": FINISH, "description": "분석을 끝내고, 사용자에게 보여 줄 실행 결과(run_id) 하나를 고른다.",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}},
                           "required": ["run_id", "reason"]}}})
        common = (PROMPT_DIR / "agent_loop.md").read_text(encoding="utf-8")
        guide_path = PROMPT_DIR / "agents" / f"{self.name}.md"
        guide = guide_path.read_text(encoding="utf-8") if guide_path.exists() else ""
        system = common.format(title=self.title, description=self.description, max_steps=max_steps,
                               required=", ".join(self.required) or "없음", guide=guide)
        user = json.dumps({"question": req.question, "user_fixed": self.user_fixed(req)}, ensure_ascii=False)
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        runs: dict[str, AgentResult] = {}
        made_by: dict[str, str] = {}       # run_id → 도구 이름
        done: set[str] = set()
        chosen: AgentResult | None = None
        for _ in range(max_steps):
            progress.now(f"{progress.TITLE.get(self.name, self.title)}: LLM이 다음 분석을 고르는 중…")
            msg, rec = ctx.llm.tool_step(messages, schema, purpose=f"agent:{self.name}")
            if msg is None:
                self.step("llm:fallback", f"LLM 판단 실패({rec.error}) → 규칙 판단")
                return None
            messages.append(msg)
            for call in msg["tool_calls"]:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError
                except ValueError:
                    args = None
                reply = self._handle(name, args, tools, runs, done, made_by)
                if reply.get("_chosen"):
                    chosen = runs[reply.pop("_chosen")]
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(reply, ensure_ascii=False, default=str)})
            if chosen is not None:
                break
        if chosen is None:
            missing = [t for t in self.required if t not in done]
            ok_ids = [rid for rid in runs if self._reportable(made_by[rid])]
            if ok_ids and not missing:
                chosen = runs[ok_ids[-1]]
                self.step("guard:auto_finish", f"{max_steps}번 안에 끝내지 않아 마지막 결과를 씀")
            else:
                self.step("llm:fallback", f"필수 단계 미완료({', '.join(missing) or '실행 없음'}) → 규칙 판단")
                return None
        chosen.payload = {**chosen.payload, "decided_by": "llm"}
        return chosen

    def _handle(self, name: str, args: dict | None, tools: dict[str, AgentTool], runs: dict[str, AgentResult],
                done: set[str], made_by: dict[str, str]) -> dict:
        """도구 호출 하나를 울타리로 검사하고 실행. LLM한테 돌려줄 dict 만듦"""
        if args is None:
            return self._reject(name, "인자가 JSON 객체가 아닙니다.")
        reason = str(args.pop("reason", "") or "")[:120]
        args = {k: v for k, v in args.items() if v is not None}   # null은 '기본값 사용'으로 봄
        if name == FINISH:
            missing = [t for t in self.required if t not in done]
            if missing:
                return self._reject(name, f"아직 필수 도구를 실행하지 않았습니다: {', '.join(missing)}")
            ok_ids = [rid for rid in runs if self._reportable(made_by[rid])]
            if args.get("run_id") not in ok_ids:
                return self._reject(name, f"보고할 수 없는 run_id입니다. 고를 수 있는 것: {', '.join(ok_ids) or '없음'}")
            if (why := self.before_finish(args["run_id"], runs)) is not None:
                return self._reject(name, why)
            self.step("llm:finish", f"{args['run_id']} — {reason}")
            return {"ok": True, "_chosen": args["run_id"]}
        tool = tools.get(name)
        if tool is None:
            return self._reject(name, f"없는 도구입니다. 쓸 수 있는 도구: {', '.join(tools)}")
        why = _validate(tool.params, args) or (tool.check(args) if tool.check else None)
        if why:
            return self._reject(name, why)
        try:
            result = tool.run(args)
        except DataNotReady as ex:
            return self._reject(name, f"필요한 데이터가 없습니다: {ex}")
        except Exception as ex:  # 도구 하나 실패해도 LLM이 다른 걸 고를 수 있게 알림
            return self._reject(name, f"실행 오류({type(ex).__name__})")
        rid = f"r{len(runs) + 1}"
        runs[rid] = result
        made_by[rid] = name
        if result.status in ("ok", "partial"):
            done.add(name)
        shown = ", ".join(f"{k}={v}" for k, v in args.items()) or "기본 조건"
        self.step(f"llm:{name}", f"{rid} ({shown}) — {reason}")
        return {"ok": True, "run_id": rid, **(tool.observe or default_observe)(result)}

    def before_finish(self, run_id: str, runs: dict[str, AgentResult]) -> str | None:
        """끝내기 전 에이전트별 점검. 막을 이유 돌려주면 finish 거부, 기본은 통과"""
        return None

    def _reportable(self, tool_name: str) -> bool:
        return not self.reportable or tool_name in self.reportable

    def _reject(self, name: str, why: str) -> dict:
        self.step("guard:reject", f"{name}: {why}")
        return {"ok": False, "error": why}


def uses(req: AnalysisRequest, dataset_id: str) -> bool:
    """탐색 에이전트가 이 자료를 골랐는지 (catalog.yaml ID). 탐색 안 거친 호출(테스트 등)은 전부 허용"""
    chosen = req.params.get("datasets")
    return chosen is None or dataset_id in chosen


def filter_places(places, req: AnalysisRequest, region: str | None = "__req__"):
    """지역·장소 목록으로 관광지 표 거름 (세 에이전트 공용). region 주면 req.region 대신 씀"""
    df = places
    region = req.region if region == "__req__" else region
    if req.place_ids:
        df = df[df["place_id"].isin(req.place_ids)]
    if region:
        if region in ("제주시", "서귀포시"):
            df = df[df["region"] == region]
        else:
            df = df[df["emd"] == region]
    return df
