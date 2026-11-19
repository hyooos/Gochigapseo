"""우선 반영 항목: 출발지·이동 시간 상한·관심사, 정보 공백 분리, 지역별 충족 표, 장소 카드, 근거 문장, 검증 에이전트."""
import dataclasses

import pandas as pd

from jeju_agent.agents.access import assess, coverage, gap_text
from jeju_agent.config import access_rules
from jeju_agent.contracts import AnalysisRequest, Intent, TravelerProfile
from jeju_agent.data.barrierfree import facility_evidence, keywords
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator, parse_origin, parse_ride
from jeju_agent.report.builder import build_markdown
from jeju_agent.report.cards import card_markdown
from jeju_agent.verify import absent_claims, forbidden_hits, verify

WHEEL = TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"])


def run(orch, intent, **kw):
    req = AnalysisRequest(question=kw.pop("question", "q"), intent=intent, **kw)
    return orch._call_agent(intent.value, req, {})


# ── 입력: 출발지·이동 시간·관심사 ───────────────────────────
def test_parse_ride_and_origin(orch):
    assert parse_ride("차에 30분 넘게 못 앉아 계셔") == 30
    assert parse_ride("차로 1시간 이상은 힘들어") == 60 and parse_ride("1시간 이상은 힘들어") is None
    assert parse_ride("2일 코스") is None
    std, rest = parse_origin("노형동에서 출발해서 서귀포 코스", orch.regions())
    assert std == "노형동" and "서귀포" in rest
    p = orch.plan("애월에 숙소 잡았어. 휠체어로 바다 보는 2일 코스, 한 번에 45분 이내").request
    assert p.origin == "애월읍" and p.profile.max_ride_min == 45 and p.profile.interests == ["sea"]
    assert p.intent == Intent.COURSE and p.region is None


def test_ride_cap_is_hard_for_every_leg(orch):
    prof = TravelerProfile(mobility="wheelchair_manual", max_ride_min=30)
    r = run(orch, Intent.COURSE, days=2, profile=prof, origin="노형동")
    legs = [p["leg_min"] for p in r.payload["itinerary"]]
    assert legs and max(legs) <= 30 and r.payload["ride_cap"] == 30
    assert r.payload["start"] == "노형동(출발지)"
    assert any("30분을 넘지 않게" in f.text for f in r.findings)


def test_origin_changes_first_leg(orch):
    a = run(orch, Intent.COURSE, days=1, profile=TravelerProfile(), origin="성산읍")
    b = run(orch, Intent.COURSE, days=1, profile=TravelerProfile(), origin="한경면")
    assert a.payload["itinerary"][0]["place_id"] != b.payload["itinerary"][0]["place_id"]


def test_interest_raises_score_but_not_verdict(repo):
    places = repo.places()
    base = assess(places, TravelerProfile())
    sea = assess(places, TravelerProfile(interests=["sea"]))
    m = base.merge(sea, on="place_id", suffixes=("", "_s"))
    beach = m[m["category"].isin(["해변", "해안·포구"])]
    assert (beach["score_s"] - beach["score"] > 0).all()
    assert (m["verdict"] == m["verdict_s"]).all()     # 관심사는 판정(안전 기준)을 바꾸지 않는다


# ── 정보 공백 분리·지역별 충족 ─────────────────────────────
def test_unknown_not_in_score(repo):
    """시설 '정보 없음'이 늘어도 점수는 그대로, 정보 없음 수만 는다."""
    p = repo.places().head(1).copy()
    full = assess(p, WHEEL).iloc[0]
    p["elevator"] = float("nan")
    p["ramp"] = float("nan")
    less = assess(p, WHEEL).iloc[0]
    assert less["score"] <= full["score"] and less["unknown_n"] >= full["unknown_n"]
    assert gap_text(less) == f"{int(less['unknown_n'])}/{int(less['checked_n'])}"


def test_coverage_table(orch):
    r = run(orch, Intent.ACCESS, profile=WHEEL)
    a4 = next(t for t in r.evidence if t.id == "A4")
    assert a4.columns[:2] == ["읍면동", "관광지 수"]
    total = sum(row[1] for row in a4.rows)
    n_all = next(f for f in r.findings if f.text.startswith("전체")).numbers["total"]
    assert total <= n_all
    cov = pd.DataFrame(r.payload["coverage"])
    assert int(cov["n"].sum()) == n_all
    assert ((cov["gap"].dropna() >= 0) & (cov["gap"].dropna() <= 1)).all()


def test_coverage_function_counts():
    a = pd.DataFrame({"emd": ["가동", "가동", "나동"], "region": ["제주시"] * 3, "verdict": ["적합", "주의", "부적합"],
                      "unknown_n": [1, 2, 0], "checked_n": [4, 4, 4]})
    c = coverage(a).set_index("group")
    assert c.loc["가동", "ok"] == 1 and c.loc["가동", "caution"] == 1 and c.loc["가동", "gap"] == 0.38
    assert c.loc["나동", "block"] == 1 and c.loc["나동", "gap"] == 0


# ── 근거 문장·장소 카드 ─────────────────────────────────
def test_facility_evidence_snippet():
    kw = keywords()
    flag, ev = facility_evidence(["3 편의시설 / 화장실: 장애인 화장실(남녀 구분)"], kw["facilities"]["acc_toilet"], kw["negative"])
    assert flag == 1 and ev.startswith("편의시설") and "장애인 화장실" in ev
    assert facility_evidence(["매표소"], kw["facilities"]["acc_toilet"], kw["negative"]) == (None, None)


def test_place_cards(orch):
    r = run(orch, Intent.ACCESS, region="서귀포시", profile=WHEEL)
    cards = {c["place_id"]: c for c in r.payload["cards"]}
    assert set(r.payload["shown_ids"]) <= set(cards)
    c = cards[r.payload["shown_ids"][0]]
    states = {f["state"] for f in c["facilities"]}
    assert states <= {"있음", "없음", "정보 없음"}
    assert all(f["evidence"] for f in c["facilities"] if f["state"] == "있음")     # 있음 판정에는 원문 근거가 붙는다
    unknown = [f["label"] for f in c["facilities"] if f["state"] == "정보 없음"]
    assert len(c["call_script"]) >= 2 and c["call_script"][-1] == access_rules()["call_questions"]["closing"]
    if unknown:
        assert len(c["call_script"]) >= 2 + len(unknown) - 1
    md = card_markdown(c)
    assert "전화로 물어볼 말" in md and "자료 기준일" in md and "출처" in md


def test_course_cards_and_access_report_first(orch):
    resp = orch.ask("휠체어로 서귀포 2일 코스 짜줘. 장애인 화장실 필요")
    assert [r.agent for r in resp.results] == ["explore", "access", "course"]
    course = resp.results[-1]
    assert [c["place_id"] for c in course.payload["cards"]] == course.payload["place_ids"]
    md = build_markdown(resp)
    assert md.index("## 접근성 진단") < md.index("## 여행 코스 설계") < md.index("## 장소 카드") < md.index("## 검증 결과")


# ── 검증 에이전트 ─────────────────────────────────────
def test_verification_passes_on_template(orch):
    for q in ("휠체어로 서귀포 갈 만한 곳 장애인 화장실 필요", "노형동에서 출발 2일 코스 30분 이내", "제주시 읍면동별 장애인 화장실 현황"):
        resp = orch.ask(q)
        assert resp.verification is not None and resp.verification.passed, (q, resp.verification)


def test_forbidden_and_absent_claims():
    assert forbidden_hits("이곳은 휠체어로 안전합니다. 걱정 마세요.") == ["안전합니다", "걱정 마세요"]
    assert forbidden_hits("방문 전 전화로 확인하세요.") == []
    assert absent_claims("엘리베이터가 없습니다.", set()) == ["엘리베이터"]
    assert absent_claims("엘리베이터 정보 없음", set()) == []
    assert absent_claims("장애인 화장실 없음 확인", {"acc_toilet"}) == []


def test_verifier_catches_course_violation(orch):
    resp = orch.ask("2일 코스 짜줘")
    course = resp.results[-1]
    broken = course.model_copy(deep=True)
    broken.payload["itinerary"][0]["verdict"] = "부적합"
    broken.payload["ride_cap"] = 1
    v = verify(resp.plan, [broken], resp.answer, resp.answer)
    bad = {c.name: c.detail for c in v.checks if not c.ok}
    assert not v.passed and "부적합 장소 포함" in bad["코스 조건"] and "1분 초과" in bad["코스 조건"]


def test_llm_answer_with_unsafe_claim_is_reverted(api_settings, repo, tmp_path):
    """LLM 설명이 숫자는 맞아도 단정 표현을 쓰면 검증 에이전트가 문장틀로 되돌린다."""
    s = dataclasses.replace(api_settings, llm_tool_calling=False)

    def fake(messages, model):
        if "JSON" in messages[0]["content"]:     # 계획(질문 해석) 호출
            return '{"intent":"access","region":"서귀포시","mobility":"wheelchair_manual","needs":["acc_toilet"]}', 10, 5
        return "조건에 맞는 곳은 모두 안전합니다. [근거표 A1]", 10, 5     # 설명 호출: 숫자는 없지만 단정 표현

    o = Orchestrator(s, repo=repo, llm=LLMClient(s, runtime_dir=tmp_path, transport=fake))
    resp = o.ask("휠체어로 서귀포 갈 만한 곳 장애인 화장실 필요")
    assert resp.answer_source == "template" and "안전합니다" not in resp.answer
    assert resp.verification.passed


# ── 실데이터 보정(11/16): 경사 판정 완화, 데이터허브 등록일 ─────────────
def test_single_steep_record_is_caution_not_block():
    from jeju_agent.contracts import TravelerProfile as TP
    places = pd.DataFrame([{"place_id": pid, "name": pid, "max_slope_deg": ms, "n_points": 10, "n_stairs": 0,
                            "n_inaccessible": 0, "n_threshold": 0, "n_rough": 0, "lat": 33.4, "lng": 126.5}
                           for pid, ms in (("one", 10.0), ("two", 9.0), ("huge", 12.0))])
    pts = pd.DataFrame([{"place_id": "one", "slope_deg": 10.0}, {"place_id": "two", "slope_deg": 9.0},
                        {"place_id": "two", "slope_deg": 8.0}, {"place_id": "huge", "slope_deg": 12.0}]).assign(stairs=0, inaccessible=0)
    a = assess(places, TP(mobility="wheelchair_manual"), points=pts).set_index("place_id")
    assert a.loc["one", "verdict"] == "주의"      # 7.2° 초과 1곳, 11.2° 미만 → 주의
    assert a.loc["two", "verdict"] == "부적합"    # 7.2° 초과 2곳
    assert a.loc["huge", "verdict"] == "부적합"   # 11.2° 이상 1곳
    old = assess(places, TP(mobility="wheelchair_manual")).set_index("place_id")   # 조사 지점이 없으면 이전 방식
    assert old.loc["one", "verdict"] == "부적합"


def test_dataset_dates_fill_only_missing():
    from jeju_agent.agents.access import date_cell
    from jeju_agent.data.barrierfree import apply_dataset_dates
    from jeju_agent.report.cards import source_line
    places = pd.DataFrame({"place_id": ["P1", "P2", "P3"], "data_date": [None, "2020-12-22", None],
                           "source_file": ["622_무장애여행정보_한림공원.zip", "700_x.csv", "999_y.csv"]})
    dates = pd.DataFrame({"data_id": ["622", "700"], "title": ["a", "b"], "registered": ["2019-09-06", "2019-01-01"],
                          "modified": ["2025-12-01", ""]})
    out, msg = apply_dataset_dates(places, dates)
    assert out["data_date"].tolist()[:2] == ["2019-09-06", "2020-12-22"] and pd.isna(out["data_date"][2])
    assert out["date_basis"].tolist()[:2] == ["등록일", "기준일자"]
    assert "등록일로 채움 1곳" in msg and "모름 1곳" in msg
    assert date_cell(out.iloc[0]) == "2019-09-06 이전(등록일)" and date_cell(out.iloc[1]) == "2020-12-22"
    assert "2019-09-06 이전 조사(데이터허브 등록일)" in source_line({"source": "s", "data_date": "2019-09-06", "date_basis": "등록일"})


def test_status_group_words(orch):
    r = orch.ask("서귀포시 분류별 정보 공백 분석").results[-1]
    assert r.evidence[0].columns[0] == "분류"
    r = orch.ask("제주시 읍면동별 장애인 화장실 현황").results[-1]
    assert r.evidence[0].columns[0] == "읍면동"
