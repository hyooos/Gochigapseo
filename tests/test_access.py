"""접근성 진단: 판정 규칙(config/access_rules.yaml)과 에이전트 결과."""
import math
from datetime import date

import pandas as pd
import pytest

from jeju_agent.agents.access import assess, stale
from jeju_agent.contracts import AnalysisRequest, Intent, TravelerProfile


def place(**kw):
    base = {"place_id": "P", "name": "가상", "lat": 33.4, "lng": 126.5, "indoor": 0, "n_points": 10, "n_stairs": 0,
            "n_threshold": 0, "n_inaccessible": 0, "n_rough": 0, "max_slope_deg": math.nan, "acc_toilet": math.nan,
            "acc_parking": math.nan, "wheelchair_rental": math.nan, "stroller_rental": math.nan, "ramp": math.nan,
            "elevator": math.nan, "braille": math.nan, "audio_guide": math.nan, "fee_won": math.nan, "data_date": "2020-12-22"}
    return {**base, **kw}


def verdict(profile, **kw):
    return assess(pd.DataFrame([place(**kw)]), profile).iloc[0]


WC = TravelerProfile(mobility="wheelchair_manual")


@pytest.mark.parametrize("slope,profile,want", [
    (4.0, WC, "적합"), (6.0, WC, "주의"), (8.0, WC, "부적합"),
    (6.0, TravelerProfile(mobility="wheelchair_power"), "적합"), (9.0, TravelerProfile(mobility="wheelchair_power"), "주의"),
    (14.0, TravelerProfile(mobility="senior"), "주의"), (16.0, TravelerProfile(mobility="senior"), "부적합"),
    (30.0, TravelerProfile(), "적합"),
])
def test_slope_thresholds(slope, profile, want):
    assert verdict(profile, max_slope_deg=slope)["verdict"] == want


def test_needed_facility_none_blocks_unknown_cautions():
    p = TravelerProfile(needs=["acc_toilet"])
    assert verdict(p, acc_toilet=0)["verdict"] == "부적합"
    r = verdict(p, acc_toilet=math.nan)
    assert r["verdict"] == "주의" and any("정보 없음" in t for _, t in r["reasons"])
    assert verdict(p, acc_toilet=1)["verdict"] == "적합"


def test_parking_not_required_for_bus_users():
    p = TravelerProfile(needs=["acc_parking"], transport="bus")
    assert verdict(p, acc_parking=0)["verdict"] == "적합"


def test_barriers_depend_on_mobility():
    assert verdict(WC, n_stairs=1)["verdict"] == "주의"
    assert verdict(TravelerProfile(), n_stairs=1)["verdict"] == "적합"
    assert verdict(WC, n_points=4, n_stairs=1, n_inaccessible=1)["verdict"] == "부적합"   # 2/4 = 0.5 이상
    assert verdict(TravelerProfile(mobility="senior"), n_rough=2)["verdict"] == "적합"


def test_fee_never_blocks_and_scores():
    """예산 기능은 없다(11/17): 입장료는 장소 밑에 보여 주기만 하고 판정에 쓰지 않는다."""
    assert verdict(TravelerProfile(), fee_won=80000)["verdict"] == "적합"
    good = verdict(WC, acc_toilet=1, wheelchair_rental=1)
    plain = verdict(WC)
    assert good["score"] > plain["score"]


def test_sorting_verdict_then_score():
    df = pd.DataFrame([place(place_id="a", name="A", max_slope_deg=9.0), place(place_id="b", name="B", n_stairs=1),
                       place(place_id="c", name="C")])
    assert assess(df, WC)["place_id"].tolist() == ["c", "b", "a"]


def test_stale():
    assert stale("2019-09-10", 5, today=date(2026, 11, 14))
    assert not stale("2024-01-01", 5, today=date(2026, 11, 14))
    assert stale(None, 5) and stale("", 5)


# ── 에이전트 ─────────────────────────────────────────────

def ask(orch, **kw):
    req = AnalysisRequest(question=kw.pop("question", "갈 만한 곳"), intent=Intent.ACCESS, **kw)
    return orch._call_agent("access", req, {})


def test_agent_wheelchair_seogwipo(orch):
    r = ask(orch, region="서귀포시", profile=TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"]))
    assert r.status == "ok" and [e.id for e in r.evidence] == ["A1", "A2", "A3", "A4"]
    names = [row[0] for row in r.evidence[0].rows]
    assert "사려니숲길-무장애나눔길" in names                       # 실데이터: 장애인화장실 기록, 기준 넘는 경사·계단 기록 없음
    verdicts = {v["name"]: v["verdict"] for v in r.payload["verdicts"]}
    assert verdicts["법환포구"] == "주의"                            # 실데이터: 7.2° 넘는 경사 기록 1곳(10°) → 주의(slope_block)
    assert verdicts["카멜리아힐"] == "부적합"                        # 실데이터: 7.2° 넘는 경사 기록 8곳
    assert all(v["region"] == "서귀포시" for v in r.payload["verdicts"])
    assert any(w.code == "STALE_DATA" for w in r.warnings)


def test_agent_auto_includes_caution_when_few(orch):
    r = ask(orch, region="구좌읍", profile=WC)
    assert r.payload["n_ok"] < 3 and r.payload["include_caution"] is True
    assert any(t.action == "rule:include_caution" for t in r.trace)


def test_agent_extend_region(orch):
    base = ask(orch, region="우도면", profile=WC)
    wide = ask(orch, region="우도면", profile=WC, params={"opts": {"extend_km": 20}})
    assert len(wide.payload["verdicts"]) > len(base.payload["verdicts"])
    assert any("지역 밖" in str(row[1]) for row in wide.evidence[0].rows)


def test_agent_unknown_region_is_insufficient(orch):
    r = ask(orch, place_ids=["없는장소"])
    assert r.status == "insufficient"


def test_agent_never_says_none_for_unknown(orch):
    """'정보 없음'을 '없음'으로 바꿔 말하지 않는다."""
    r = ask(orch, profile=TravelerProfile(needs=["acc_toilet"]))
    unk = [f for f in r.findings if "언급이 없어" in f.text]
    assert unk and all("없습니다" not in f.text.split("언급이 없어")[0] for f in unk)


def test_survey_recommendation_companion_and_no():
    """코스 조사자의 추천 대상: Y(동반)은 '주의'(점수 그대로), N은 부적합, Y·빈칸은 영향 없음. 일행이 있으면 동반 조건 충족."""
    base = verdict(WC)
    comp = verdict(WC, rec_wheelchair_manual="Y(동반)")
    assert (base["verdict"], comp["verdict"]) == ("적합", "주의") and comp["score"] == base["score"]
    assert ("companion", "보호자·동행인 동반 시 추천(수동 휠체어, 조사자 의견)") in comp["reasons"]
    assert verdict(WC, rec_wheelchair_manual="N")["verdict"] == "부적합"
    assert verdict(WC, rec_wheelchair_manual="Y")["verdict"] == "적합"
    assert verdict(TravelerProfile(mobility="wheelchair_manual", companions=2), rec_wheelchair_manual="Y(동반)")["verdict"] == "적합"
    assert verdict(TravelerProfile(mobility="stroller"), rec_infant="N")["verdict"] == "부적합"      # 유아차 → 영유아 열


def test_key_checks_flag_missing_slope_without_changing_verdict(orch):
    """이동 조건별 확인할 점: 판정은 그대로, 경사 기록이 없으면 '방문 전 추가 확인 필요' + 그 질문을 전화 문의 맨 앞에."""
    from jeju_agent.agents.access import key_checks
    from jeju_agent.config import access_rules
    rules = access_rules()
    wc = TravelerProfile(mobility="wheelchair_manual")
    no_slope = {"max_slope_deg": math.nan, "n_points": 5, "n_stairs": 0, "n_inaccessible": 1}
    kc = key_checks(no_slope, wc, rules)
    assert [(i["key"], i["state"]) for i in kc["items"]] == [("slope", "unknown"), ("stairs", "ok"), ("inaccessible", "warn")]
    assert kc["need_call"] and kc["title"] == "경사도·계단·접근 불가 지점 확인"
    senior = key_checks({**no_slope, "max_slope_deg": 3.0}, TravelerProfile(mobility="senior"), rules)
    assert [i["key"] for i in senior["items"]] == ["slope"] and not senior["need_call"]
    assert key_checks(no_slope, TravelerProfile(), rules) is None              # 이동 조건이 없으면 표시하지 않음
    r = orch.execute(orch.plan_form("q", {"profile": wc, "region": "제주 전체", "intent": "access"}), {})
    a = r.results[-1]
    card = next(c for c in a.payload["cards"] if c["key_checks"]["need_call"])
    assert card["call_script"][1] == rules["call_questions"]["slope_unknown_wheel"]
    v = {x["place_id"]: x["verdict"] for x in a.payload["verdicts"]}
    assert card["verdict"] == v[card["place_id"]] and r.verification.passed
