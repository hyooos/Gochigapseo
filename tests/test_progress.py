"""분석 과정 알림(progress), 맞춤 여행 → 물어보기 이어 묻기, 발달·지적장애 조건."""
from jeju_agent import progress
from jeju_agent.contracts import TravelerProfile
from jeju_agent.orchestrator import parse_profile
from jeju_agent.privacy import scrub

WHEEL = TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"])


def course_form(**kw):
    return {"profile": WHEEL, "days": 2, "region": "서귀포시", "intent": "course", **kw}


# ── 분석 과정 알림 ────────────────────────────────────────
def test_live_steps_follow_the_real_run_and_are_kept_on_response(orch):
    live: list[progress.Step] = []
    with progress.listen(live.append):
        r = orch.execute(orch.plan_form("서귀포 2일 코스", course_form()), {})
    done = [s.text for s in live if s.kind == "done"]
    now = [s.text for s in live if s.kind == "now"]
    assert done == r.progress                                   # 화면에 흘러간 줄 = 결과에 남은 줄
    order = ["질문 이해", "자료 찾기", "자료 고르기", "자료 점검", "접근성 판정", "코스 배치", "검증"]
    pos = [next(i for i, t in enumerate(done) if k in t) for k in order]
    assert pos == sorted(pos)                                   # 찾기 → 고르기 → 점검 → 분석 → 검증 순서
    assert {"데이터 탐색 중…", "코스 설계 중…", "접근성 진단 중…", "검증 중…"} <= set(now)
    v = r.verification
    assert done[-1] == f"✓ 검증 — {sum(c.ok for c in v.checks)}/{len(v.checks)} 통과"   # 숫자는 실제 검증 결과 그대로


def test_progress_has_no_user_condition_words(orch):
    r = orch.execute(orch.plan_form("q", course_form(profile=TravelerProfile(mobility="wheelchair_manual", cognition="intellectual"))), {})
    text = "\n".join(r.progress)
    assert r.progress and "휠체어" not in text and "발달" not in text


def test_listener_errors_do_not_stop_analysis(orch):
    def boom(_):
        raise RuntimeError("화면 오류")
    with progress.listen(boom):
        r = orch.execute(orch.plan_form("q", {"intent": "status", "region": "제주 전체"}), {})
    assert r.results and r.progress and any("현황 집계" in t for t in r.progress)


def test_no_listener_no_cost():
    progress.emit("access", "assess", "x")      # 듣는 쪽이 없으면 아무 일도 없다
    assert progress.friendly("course", "end", "") is None and progress.friendly("orchestrator", "call:access", "ok") is None
    assert progress.friendly("course", "guard:reject", "select_pace: 범위 밖").text.startswith("🛡 안전 울타리")


# ── 발달·지적장애 ─────────────────────────────────────────
def test_cognition_parsed_and_scrubbed():
    assert parse_profile("발달장애가 있는 아들과 애월 2일 코스")[0] == {"cognition": "intellectual"}
    assert parse_profile("자폐가 있는 조카랑 갈 만한 곳")[0]["cognition"] == "intellectual"
    assert scrub("지적장애 조건이라 실내") == "(조건) 조건이라 실내"
    assert "발달·지적장애" in TravelerProfile(cognition="intellectual").describe()


def test_cognition_uses_survey_column_and_companion_rule(orch):
    def run(n):
        req = orch.plan_form("q", {"profile": TravelerProfile(cognition="intellectual", companions=n), "intent": "access",
                                    "region": "제주 전체"}).request
        return orch._call_agent("access", req, {})
    alone, together = run(1), run(3)
    assert alone.payload["n_caution"] > together.payload["n_caution"]       # 혼자면 '동반 시 추천' 코스가 주의로
    assert any("발달·지적장애: 보호자·동행인 동반 시 추천" in c["survey"] for c in alone.payload["cards"])
    assert any("발달·지적장애" in t for t in alone.limitations)              # 코스 조사에만 있다는 한계를 밝힌다
    assert not any("발달·지적장애" in t for t in run_plain(orch).limitations)


def run_plain(orch):
    req = orch.plan_form("q", {"profile": TravelerProfile(), "intent": "access", "region": "제주 전체"}).request
    return orch._call_agent("access", req, {})


def test_profile_ask_offers_cognition(orch):
    plan = orch.plan("애월에서 갈 만한 곳", {}, ask_profile=True)
    labels = [c["label"] for c in plan.request.params["edit"]["choices"]]
    assert "발달·지적장애" in labels
    q = next(c["q"] for c in plan.request.params["edit"]["choices"] if c["label"] == "발달·지적장애")
    assert orch.plan(q, {}).request.profile.cognition == "intellectual"
