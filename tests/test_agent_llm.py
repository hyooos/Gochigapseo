"""하위 에이전트 LLM 판단 + 규칙 울타리 테스트.

가짜 모델이 정해진 순서로 도구를 고른다(네트워크 없음). 확인하는 것:
- LLM이 고른 조건이 코드 실행에 반영되고, finish로 고른 결과가 보고된다
- 울타리가 근거 없는 선택(기본 결과 전 조건 변경, 근거 없는 주의 포함·지역 확장, 여유가 필요한 이용자의 '보통' 등)을 거부한다
- LLM이 실패하면 규칙 판단으로 같은 결과를 낸다
- 다른 에이전트가 부른 호출(called_by)은 LLM을 쓰지 않는다
- 모델에게 돌아가는 관찰값에 장소별 원본 값(payload)이 없다
"""
import dataclasses
import json
import types

from jeju_agent.contracts import AnalysisRequest, Intent, TravelerProfile
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator


def _setup(api_settings, repo, tmp_path, script: list[list[tuple[str, dict]]]):
    """script[i] = i번째 LLM 호출에서 모델이 고를 (도구, 인자) 목록. 다 쓰면 예외 → LLM 실패."""
    s = dataclasses.replace(api_settings, llm_cache=False, llm_max_calls_per_question=12, agent_llm=True,
                            llm_tool_calling=False, agent_max_steps=4)
    llm = LLMClient(s, runtime_dir=tmp_path)
    seen: list[list[dict]] = []
    step = {"i": 0}

    def fake_tool(messages, tools):
        seen.append(json.loads(json.dumps(messages, default=str)))
        i = step["i"]
        step["i"] += 1
        if i >= len(script):
            raise RuntimeError("script exhausted")
        calls = [types.SimpleNamespace(id=f"c{i}_{k}", function=types.SimpleNamespace(name=n, arguments=json.dumps(a, ensure_ascii=False)))
                 for k, (n, a) in enumerate(script[i])]
        return types.SimpleNamespace(tool_calls=calls, content=""), 100, 10

    llm._openai_tool_request = fake_tool
    llm._openai_call = lambda messages, model: ("설명", 100, 10)
    return Orchestrator(s, repo=repo, llm=llm), seen


def _replies(seen) -> list[dict]:
    return [json.loads(m["content"]) for m in seen[-1] if m["role"] == "tool"] if seen else []


def _actions(result) -> list[str]:
    return [t.action for t in result.trace]


WC = TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"])


def _access(o, **kw):
    req = AnalysisRequest(question=kw.pop("question", "갈 만한 곳"), intent=Intent.ACCESS, **kw)
    return o._call_agent("access", req, {})


# ── 접근성 진단 ──────────────────────────────────────────

def test_llm_runs_screen_and_finishes(api_settings, repo, tmp_path):
    o, seen = _setup(api_settings, repo, tmp_path, [
        [("screen", {"reason": "기본 조건으로 먼저 본다"})],
        [("finish", {"run_id": "r1", "reason": "기본 결과로 충분"})],
    ])
    r = _access(o, region="서귀포시", profile=WC)
    assert r.status == "ok" and r.payload["decided_by"] == "llm"
    assert "llm:screen" in _actions(r) and "llm:finish" in _actions(r)
    obs = _replies(seen)[0]
    assert obs["ok"] and "verdicts" not in json.dumps(obs, ensure_ascii=False)   # 장소별 원본 값은 보내지 않음


def test_guard_requires_base_run_first(api_settings, repo, tmp_path):
    o, seen = _setup(api_settings, repo, tmp_path, [
        [("screen", {"include_caution": True, "reason": "처음부터 넓게"})],
        [("screen", {"reason": "기본"})],
        [("finish", {"run_id": "r1", "reason": "끝"})],
    ])
    r = _access(o, region="서귀포시", profile=WC)
    assert "guard:reject" in _actions(r) and r.payload["decided_by"] == "llm"


def test_guard_blocks_caution_when_enough_suitable(api_settings, repo, tmp_path):
    o, seen = _setup(api_settings, repo, tmp_path, [
        [("screen", {"reason": "기본"})],
        [("screen", {"include_caution": True, "reason": "더 많이"})],
        [("finish", {"run_id": "r1", "reason": "끝"})],
    ])
    r = _access(o, profile=TravelerProfile())        # 조건 없음 → 적합이 충분
    rej = [t.detail for t in r.trace if t.action == "guard:reject"]
    assert rej and "충분" in rej[0]


def test_extend_allowed_only_when_few_candidates(api_settings, repo, tmp_path):
    o, _ = _setup(api_settings, repo, tmp_path, [
        [("screen", {"reason": "기본"})],
        [("screen", {"extend_km": 20, "reason": "우도 안 후보가 적어 넓힌다"})],
        [("finish", {"run_id": "r2", "reason": "넓힌 결과"})],
    ])
    r = _access(o, region="우도면", profile=WC)
    assert r.payload["extend_km"] == 20 and "guard:reject" not in _actions(r)
    o2, _ = _setup(api_settings, repo, tmp_path / "b", [
        [("screen", {"reason": "기본"})],
        [("screen", {"extend_km": 10, "reason": "넓혀 본다"})],
        [("finish", {"run_id": "r1", "reason": "끝"})],
    ])
    r2 = _access(o2, region="서귀포시", profile=TravelerProfile())
    assert "guard:reject" in _actions(r2) and r2.payload["extend_km"] == 0


def test_llm_failure_falls_back_to_rules(api_settings, repo, tmp_path):
    o, _ = _setup(api_settings, repo, tmp_path, [])
    r = _access(o, region="서귀포시", profile=WC)
    base = Orchestrator(o.s, repo=repo, llm=LLMClient(dataclasses.replace(o.s, agent_llm=False), runtime_dir=tmp_path / "b"))
    rule = _access(base, region="서귀포시", profile=WC)
    assert "llm:fallback" in _actions(r) and "decided_by" not in r.payload
    assert r.evidence[0].rows == rule.evidence[0].rows


# ── 코스 설계 ────────────────────────────────────────────

def _course(o, **kw):
    req = AnalysisRequest(question=kw.pop("question", "코스 짜줘"), intent=Intent.COURSE, **kw)
    return o._call_agent("course", req, {})


def test_course_guard_keeps_relaxed_pace(api_settings, repo, tmp_path):
    o, _ = _setup(api_settings, repo, tmp_path, [
        [("plan", {"reason": "기본"})],
        [("plan", {"pace": "보통", "reason": "더 많이 보자"})],
        [("finish", {"run_id": "r1", "reason": "끝"})],
    ])
    r = _course(o, days=2, profile=WC)
    rej = [t.detail for t in r.trace if t.action == "guard:reject"]
    assert rej and "여유" in rej[0] and r.payload["pace"] == "여유"


def test_course_llm_picks_conditions_from_question(api_settings, repo, tmp_path):
    """D40 A: 실내·이동 상한은 LLM이 질문을 보고 정한다(날씨 위험이 없어도, 기본 결과를 먼저 보지 않아도)."""
    o, _ = _setup(api_settings, repo, tmp_path, [
        [("plan", {"indoor_per_day": 2, "max_leg_min": 30, "reason": "질문이 실내 위주, 멀미"})],
        [("finish", {"run_id": "r1", "reason": "질문에 맞음"})],
    ])
    r = _course(o, days=1, month=10, question="박물관 같은 실내 위주로, 멀미가 있어 짧게 이동")   # 10월은 비 위험이 낮다
    assert "guard:reject" not in _actions(r)
    assert r.payload["indoor_per_day"] == 2 and r.payload["max_leg_min"] <= 30 and r.payload["decided_by"] == "llm"


def test_course_safety_guards_still_hold(api_settings, repo, tmp_path):
    """울타리는 안전·이용자가 정한 조건만: 이동 상한(말한 분)·실내 원함·플랜 두 번까지."""
    prof = TravelerProfile(mobility="walker", needs=["indoor_pref"], max_ride_min=30)
    o, _ = _setup(api_settings, repo, tmp_path, [
        [("plan", {"max_leg_min": 60, "reason": "넓게"}), ("plan", {"indoor_per_day": 0, "reason": "실외"})],
        [("plan", {"reason": "기본"})],
        [("plan", {"pace": "보통", "reason": "다시"})],
        [("plan", {"reason": "세 번째"}), ("finish", {"run_id": "r2", "reason": "끝"})],
    ])
    r = _course(o, days=1, profile=prof)
    rej = [t.detail for t in r.trace if t.action == "guard:reject"]
    assert any("30분" in x for x in rej) and any("실내" in x for x in rej) and any("2번까지" in x for x in rej)


TIGHT = TravelerProfile(mobility="wheelchair_manual", max_ride_min=30)   # 가상 데이터 서귀포 3일: 화장실 확인 장소가 없는 날이 생김


def test_course_recheck_requires_one_retry_before_finish(api_settings, repo, tmp_path):
    """D40 B: 점검에서 고칠 수 있는 문제(fix)가 있으면 다시 짜 보기 전에는 끝내지 못한다."""
    o, seen = _setup(api_settings, repo, tmp_path, [
        [("plan", {"reason": "기본"})],
        [("finish", {"run_id": "r1", "reason": "끝"})],
        [("plan", {"toilet_per_day": 1, "reason": "점검: 화장실 확인 장소가 없는 날"})],
        [("finish", {"run_id": "r2", "reason": "화장실 없는 날이 줄어듦"})],
    ])
    r = _course(o, days=3, region="서귀포시", profile=TIGHT)
    first = next(json.loads(m["content"]) for m in seen[1] if m["role"] == "tool")
    assert any(i["code"] == "toilet_day" and i["fix"] == {"toilet_per_day": 1} for i in first["issues"])
    rej = [t.detail for t in r.trace if t.action == "guard:reject"]
    assert rej and "다시 짜" in rej[0]
    assert r.payload["toilet_per_day"] == 1 and len(r.payload["toilet_missing_days"]) < len(first["issues"][0]["days"])


def test_course_rule_path_rechecks_once(orch):
    """키 없음(규칙): 같은 점검 → 한 번 다시 짜고, 문제가 적은 코스를 쓴다."""
    req = AnalysisRequest(question="코스 짜줘", intent=Intent.COURSE, days=3, region="서귀포시", profile=TIGHT)
    r = orch._call_agent("course", req, {})
    acts = [t.action for t in r.trace]
    assert acts.count("rule:recheck") == 1 and "rule:recheck_pick" in acts
    assert r.payload["toilet_per_day"] == 1 and not any(i["code"] == "toilet_day" for i in r.payload["issues"])


def test_access_called_by_course_uses_rules(api_settings, repo, tmp_path):
    """코스 에이전트가 부른 접근성 진단은 LLM을 쓰지 않는다(호출이 겹겹이 늘지 않게)."""
    o, seen = _setup(api_settings, repo, tmp_path, [
        [("plan", {"reason": "기본"})],
        [("finish", {"run_id": "r1", "reason": "끝"})],
    ])
    _course(o, days=1)
    assert len(seen) == 2                      # 코스 판단 2번뿐 (접근성 진단 판단 호출 없음)
    calls = [t for t in o._trace if t.action == "call:access"]
    assert calls


# ── 현황 ─────────────────────────────────────────────────

def test_status_llm_changes_grouping(api_settings, repo, tmp_path):
    o, _ = _setup(api_settings, repo, tmp_path, [
        [("summarize", {"reason": "기본"})],
        [("summarize", {"group_by": "category", "reason": "관광지 종류별로 보자"})],
        [("finish", {"run_id": "r2", "reason": "질문이 종류를 물음"})],
    ])
    r = o._call_agent("status", AnalysisRequest(question="어떤 종류 관광지가 부족해?", intent=Intent.STATUS), {})
    assert r.payload["group_by"] == "category" and r.payload["decided_by"] == "llm"
