"""대화 이어가기 평가 (여러 턴 시나리오) → outputs/dialog_report.md

    python scripts/eval_dialog.py          # 오프라인
    python scripts/eval_dialog.py --api    # LLM 사용 (비용 나감)
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import llm_record  # noqa: E402  (scripts/llm_record.py)

from jeju_agent.config import OUTPUT_DIR, get_settings  # noqa: E402
from jeju_agent.contracts import TravelerProfile  # noqa: E402
from jeju_agent.llm import LLMClient  # noqa: E402
from jeju_agent.orchestrator import Orchestrator  # noqa: E402


def days_of(resp) -> dict[int, list[str]]:
    r = resp.results[-1] if resp.results else None
    it = (r.payload.get("itinerary") if r else None) or []
    return {d: [p["name"] for p in it if p["day"] == d] for d in sorted({p["day"] for p in it})}


def kind(resp) -> str:
    e = resp.plan.request.params.get("edit") or {}
    return e.get("kind") or resp.plan.request.intent.value


# 턴 검사: (이름, 함수(resp, prev_days, mem) -> bool)
def is_(*kinds):
    return " 또는 ".join(kinds), lambda r, prev, mem: kind(r) in kinds


def same_day(d: int):
    return f"{d}일차 그대로", lambda r, prev, mem: days_of(r).get(d) == prev.get(d) if days_of(r) else True


def changed_day(d: int):
    return f"{d}일차 바뀜", lambda r, prev, mem: not days_of(r) or days_of(r).get(d) != prev.get(d)


def n_days(n: int):
    return f"{n}일 유지", lambda r, prev, mem: r.plan.request.days == n


def course_unchanged():
    return "코스 그대로", lambda r, prev, mem: not days_of(r) or days_of(r) == prev


def has_meals():
    return "식당·숙소 넣음", lambda r, prev, mem: bool(r.results and r.results[-1].payload.get("extras"))


def near_course():
    return "지금 코스 근처", lambda r, prev, mem: set(r.plan.request.place_ids) == {p for ps in [mem.get("course_ids", [])] for p in ps}


WALK_BLIND = {"mobility": "walker", "vision": "blind"}
SCENARIOS = [
    {"name": "시연 PDF 1 (지팡이·전맹·실내·택시, 2일)",
     "form": {"profile": {**WALK_BLIND, "needs": ["indoor_pref"], "transport": "taxi"}, "days": 2, "region": "제주 전체"},
     "turns": [("2일차만 바꿔줘볼래?", [is_("ask"), course_unchanged()]),
               ("다른 곳으로", [is_("day_swap"), same_day(1), changed_day(2)]),
               ("1일차가 너무 빡빡해. 1일차 일정만 바꿔줘", [is_("lighter"), same_day(2)]),
               ("아니 괜찮아", [is_("ask"), course_unchanged()])]},
    {"name": "시연 PDF 2 (지팡이·전맹·자주 쉬기·2명, 2일)",
     "form": {"profile": {**WALK_BLIND, "needs": ["rest_often"], "companions": 2}, "days": 2, "region": "제주 전체"},
     "turns": [("1일차 너무 코스가 서로 먼것같아. 1일차만 다른 코스로 추천해줄래?", [is_("day_swap"), same_day(2)]),
               ("그럼 여행코스에 숙소 하나랑 각각 점심 저녁 식당까지 포함해서 짜줄래?", [is_("course"), n_days(2), course_unchanged(), has_meals()]),
               ("혹시 주변 식당이랑 숙소까지 고려해줄 수 있어?", [is_("nearby"), near_course()])]},
    {"name": "자유 말투 (수동 휠체어·화장실, 서귀포 2일)",
     "form": {"profile": {"mobility": "wheelchair_manual", "needs": ["acc_toilet"]}, "days": 2, "region": "서귀포시"},
     "turns": [("둘째 날은 좀 덜 걷는 데로 바꿔줘", [is_("day_swap", "ask"), same_day(1)]),
               ("첫날에 하나만 더 넣어줘", [is_("day_count", "ask"), same_day(2)]),
               ("엄마도 같이 가기로 했어", [is_("join")]),
               ("제주시 읍면동별 장애인 화장실 현황도 볼 수 있어?", [is_("status")]),
               ("방금 거 보고서로 정리해줘", [is_("report")])]},
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", action="store_true", help="LLM API 모드로 실행(비용 발생)")
    ap.add_argument("--record", action="store_true", help="(--api와 함께) 실제 모델 답을 outputs/llm_record_dialog.jsonl에 남김(캐시 끔)")
    ap.add_argument("--replay", help="남겨 둔 llm_record 파일로 키 없이 같은 흐름 재생(도구 선택은 규칙)")
    a = ap.parse_args(argv)
    s = get_settings()
    if a.replay:   # 키 없이 남겨 둔 모델 답으로 (실제 호출 없음)
        s = dataclasses.replace(s, llm_mode="api", llm_api_key="replay", llm_cache=False, agent_llm=False)
    elif not a.api:
        s = dataclasses.replace(s, llm_mode="offline")
    elif a.record:
        s = dataclasses.replace(s, llm_cache=False)
    if a.replay:
        replay = llm_record.Replay(Path(a.replay))
        orch = Orchestrator(s, llm=LLMClient(s, transport=replay))
    else:
        orch = Orchestrator(s)
        if a.api and a.record:
            llm_record.attach(orch.llm, OUTPUT_DIR / "llm_record_dialog.jsonl")
    lines = [f"# 대화 이어가기 평가 ({'API · ' + s.llm_model if orch.llm.enabled else '오프라인(규칙)'})", ""]
    n_turn = n_ok = calls = 0
    cost, t0 = 0.0, time.perf_counter()
    for sc in SCENARIOS:
        mem: dict = {}
        f = sc["form"]
        resp = orch.execute(orch.plan_form(f"{f['region']} {f['days']}일 여행 코스", {
            "profile": TravelerProfile(**f["profile"]), "days": f["days"], "region": f["region"], "intent": "course"}), mem)
        prev = days_of(resp)
        lines += [f"## {sc['name']}", "", f"시작 코스: {prev}", "", "| 이용자 말 | 읽은 결과 | 검사 | 통과 | 호출 |", "|---|---|---|---|---|"]
        for q, checks in sc["turns"]:
            mem["course_ids"] = list(mem.get("last_place_ids") or [])
            r = orch.execute(orch.plan(q, mem, ask_profile=True), mem)
            res = [(name, bool(fn(r, prev, mem))) for name, fn in checks]
            ok = all(v for _, v in res)
            n_turn += 1
            n_ok += ok
            u = r.llm_usage
            calls += u.calls
            cost += u.est_cost_usd
            read = kind(r) + (f" ← '{r.plan.request.question}'" if r.plan.request.params.get("said") else "")
            lines.append(f"| {q} | {read} | {', '.join(f'{n}{'' if v else '✗'}' for n, v in res)} | {'✅' if ok else '❌'} | {u.calls} |")
            if days_of(r):
                prev = days_of(r)
        lines.append("")
    head = [f"- 통과: {n_ok}/{n_turn}턴 ({n_ok / max(n_turn, 1):.0%})",
            f"- LLM 호출 {calls}회 · 예상 비용 ${cost:.4f} (턴당 평균 ${cost / max(n_turn, 1):.5f}) · {time.perf_counter() - t0:.1f}초", ""]
    text = "\n".join(lines[:2] + head + lines[2:])
    print(text)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "dialog_report.md").write_text(text, encoding="utf-8")
    return 0 if n_ok == n_turn else 1


if __name__ == "__main__":
    sys.exit(main())
