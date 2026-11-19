"""분석 과정 알림: 에이전트 기록을 쉬운 말 한 줄로 바꿔서 화면에 실시간으로 보냄
기록은 privacy.scrub 거친 글이라 이용자 조건 낱말 없음
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    kind: str    # "now" = 지금 하는 일, "done" = 끝난 단계
    text: str


_listeners: ContextVar[tuple[Callable[[Step], None], ...]] = ContextVar("progress_listeners", default=())

TITLE = {"explore": "데이터 탐색", "access": "접근성 진단", "course": "코스 설계", "status": "현황 분석"}
INTENT_KO = {"access": "조건에 맞는 장소 찾기", "course": "여행 코스 짜기", "status": "무장애 관광 현황",
             "report": "보고서 정리", "unsupported": "답할 수 있는지 확인", "nearby": "근처 식당·카페·숙소 찾기"}
PARSER_KO = {"form": "화면에서 고른 조건", "rule": "규칙 해석", "llm": "LLM 해석"}


@contextmanager
def listen(fn: Callable[[Step], None]) -> Iterator[None]:
    """이 블록 안 알림을 fn으로 받음. 겹쳐 쓰면 바깥 listener도 같이 받음"""
    token = _listeners.set(_listeners.get() + (fn,))
    try:
        yield
    finally:
        _listeners.reset(token)


def now(text: str) -> None:
    """지금 하는 일 알림 (예: 'LLM이 설명을 쓰는 중…'). trace엔 안 남김"""
    _send(Step("now", text))


def emit(agent: str, action: str, detail: str = "") -> None:
    """기록 한 줄을 쉬운 말로 알림. 화면에 필요 없는 기록(시작·끝 등)은 건너뜀"""
    if not _listeners.get():
        return
    step = friendly(agent, action, detail or "")
    if step is not None:
        _send(step)


def _send(step: Step) -> None:
    for fn in _listeners.get():
        try:
            fn(step)
        except Exception:   # 알림 실패해도 분석은 안 멈춤
            pass


def friendly(agent: str, action: str, detail: str) -> Step | None:
    """기록(agent, action, detail) → 화면 한 줄. 모르는 건 그대로 짧게"""
    title = TITLE.get(agent, agent)
    if action == "start":
        return Step("now", f"{title} 중…") if agent in TITLE else None
    if action == "end" or action.startswith("call:") or action == "llm:finish":
        return None
    if agent == "orchestrator" and action == "plan":
        intent, _, parser = detail.partition(" by ")
        return Step("done", f"✓ 질문 이해 — {INTENT_KO.get(intent, intent)} ({PARSER_KO.get(parser, parser)})")
    if action == "search_catalog":
        return Step("done", f"✓ 자료 찾기 — 데이터 목록에서 {detail}")
    if action == "select":
        m = re.match(r"고름 (\d+)개.*?/ 뺌 (\d+)개", detail)
        return Step("done", f"✓ 자료 고르기 — {m.group(1)}개를 골랐어요" + (f" ({m.group(2)}개는 뺐어요 — 이유는 근거표 D2)"
                                                                     if int(m.group(2)) else "")
                    if m else f"✓ 자료 고르기 — {detail}")
    if action == "inspect":
        parts = [p.strip() for p in detail.split(",") if p.strip()]
        missing = sum(1 for p in parts if not p.endswith("=있음"))
        return Step("done", f"✓ 자료 점검 — {len(parts)}개 자료의 행 수·빈칸·기준일 확인"
                    + (f", {missing}개는 센터 CSV가 필요해요" if missing else ""))
    if action == "fill:visitjeju":
        return Step("done", f"✓ 자료 보완 — {detail}")
    if action == "assess":
        return Step("done", f"✓ 접근성 판정 — {detail}")
    if action == "extend":
        return Step("done", f"✓ 범위 넓히기 — {detail}")
    if action == "weather":
        return Step("done", f"✓ 날씨 집계 — {detail}")
    if action == "schedule":
        return Step("done", f"✓ 코스 배치 — {detail}")
    if action == "extras":
        return Step("done", f"✓ 점심·숙소 배치 — {detail}")
    if action == "aggregate":
        return Step("done", f"✓ 현황 집계 — {detail}")
    if action.startswith("rule:"):
        return Step("done", f"✓ 규칙 판단({title}) — {detail}")
    if action.startswith("edit:"):
        return Step("done", f"✓ 고친 내용 반영 — {detail}")
    if action == "guard:reject":
        return Step("done", f"🛡 안전 울타리 — LLM 제안을 거부했어요: {detail}")
    if action == "guard:auto_finish":
        return Step("done", f"🛡 안전 울타리 — {detail}")
    if action == "llm:fallback":
        return Step("done", f"↩ 규칙 판단으로 전환({title}) — {detail}")
    if action.startswith("llm:"):
        why = detail.split(" — ", 1)[-1]
        return Step("done", f"🤖 LLM 판단({title}) — {action[4:]}: {why}")
    if action in ("explain", "tool_answer"):
        return Step("done", "✓ 설명 쓰기 — " + ("LLM 설명을 숫자·근거표와 대조해 채택" if detail.startswith("llm") else
                                             f"문장틀 사용({detail.removeprefix('문장틀 사용(').rstrip(')')})"))
    if action == "tool_call":
        return Step("done", "✓ LLM 도구 선택 — " + (detail if not detail.startswith("fallback") else "실패해 규칙 실행으로 전환"))
    if agent == "verifier" and action == "verify":
        return Step("done", f"✓ 검증 — {detail}")
    if agent == "verifier" and action == "revert":
        return Step("done", f"↩ 검증 — {detail}")
    if action == "verify":
        return Step("done", f"✓ 검증 — {detail}")
    return Step("done", f"✓ {title} — {detail}" if detail else f"✓ {title} — {action}")
