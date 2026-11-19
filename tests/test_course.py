"""코스 설계: 속도 규칙, 부적합 제외, 이동 시간 상한, 날씨·버스(센터 CSV) 반영."""
import dataclasses
import shutil
from pathlib import Path

import pandas as pd
import pytest

from jeju_agent.config import access_rules
from jeju_agent.contracts import AnalysisRequest, Intent, PlaceItem, TravelerProfile
from jeju_agent.data import Repository
from jeju_agent.data import kto
from jeju_agent.data.center import nearest_stops, weather_month_stats
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator


def plan(orch, **kw):
    req = AnalysisRequest(question=kw.pop("question", "코스 짜줘"), intent=Intent.COURSE, **kw)
    return orch._call_agent("course", req, {})


def test_relaxed_pace_for_wheelchair_and_no_blocked_places(orch):
    prof = TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"])
    r = plan(orch, days=2, profile=prof)
    assert r.payload["pace"] == "여유"
    per_day = access_rules()["course"]["pace"]["여유"]["per_day"]
    days = pd.DataFrame(r.payload["itinerary"]).groupby("day").size()
    assert (days <= per_day).all()
    assert all(p["verdict"] != "부적합" for p in r.payload["itinerary"])
    acc = orch._call_agent("access", AnalysisRequest(question="q", intent=Intent.ACCESS, profile=prof), {})
    blocked = {v["place_id"] for v in acc.payload["verdicts"] if v["verdict"] == "부적합"}
    assert blocked and not blocked & set(r.payload["place_ids"])


def test_default_pace_and_leg_limits(orch):
    r = plan(orch, days=2)
    assert r.payload["pace"] == "보통"
    limit = r.payload["max_leg_min"]
    for p in r.payload["itinerary"]:
        assert p["leg_min"] <= (limit * 1.5 if p["order"] == 1 else limit)


def test_rainy_month_adds_indoor(orch):
    r = plan(orch, days=2, month=7)               # 가상 날씨: 7월 비 온 날 비율이 높게 만들어져 있다
    assert r.payload["weather"] == "ok" and r.payload["rain_ratio"] >= access_rules()["weather"]["rain_ratio_high"]
    assert r.payload["indoor_per_day"] == 1 and "C3" in [e.id for e in r.evidence]
    it = pd.DataFrame(r.payload["itinerary"])
    acc = {v["place_id"]: v["indoor"] for v in orch._call_agent(
        "access", AnalysisRequest(question="q", intent=Intent.ACCESS), {}).payload["verdicts"]}
    for _, g in it.groupby("day"):
        assert any(acc[pid] == 1 for pid in g["place_id"])


def test_no_month_and_no_weather_data_are_told(orch, settings, tmp_path):
    r = plan(orch, days=1)
    assert any(w.code == "NO_MONTH" for w in r.warnings)
    # 센터 날씨 자료가 없는 환경 (관광지 자료만 있음)
    d = tmp_path / "data" / "public"
    d.mkdir(parents=True)
    for f in ("places.csv", "place_points.csv"):
        shutil.copy(orch.repo.base / f, d / f)
    o2 = Orchestrator(settings, repo=Repository("sample", data_dir=tmp_path / "data"), llm=LLMClient(settings, runtime_dir=tmp_path / "rt"))
    r2 = plan(o2, days=1, month=7)
    assert r2.status in ("ok", "partial") and any(w.code == "NO_WEATHER" for w in r2.warnings)


def test_bus_users_get_stop_distances(orch):
    r = plan(orch, days=1, profile=TravelerProfile(mobility="stroller", transport="bus"))
    c4 = next(e for e in r.evidence if e.id == "C4")
    assert len(c4.rows) == len(r.payload["itinerary"]) and all(isinstance(row[2], int) for row in c4.rows)


def test_follow_up_place_ids_limit_candidates(orch):
    ids = ["BF-" + "x"]  # 없는 장소 → 후보 없음
    r = plan(orch, days=1, place_ids=ids)
    assert r.status == "insufficient"


def test_weather_stats_and_stops_are_aggregates(repo):
    stats = weather_month_stats(repo.get("weather_daily").df, 1, access_rules()["weather"])
    assert set(stats.columns) == {"observatory_code", "n_days", "rain_ratio", "wind_ratio", "hot_ratio", "years"}
    assert stats["rain_ratio"].between(0, 1).all() and (stats["n_days"] == 62).all()
    st = nearest_stops(repo.places(), repo.get("bus_stops").df)
    # 장소마다 가장 가까운 정류소 1곳(이름·거리, 지도 표시용 좌표)만. 정류소 표 전체를 내지 않는다
    assert list(st.columns) == ["place_id", "stop_nm", "stop_m", "stop_lat", "stop_lng"] and (st["stop_m"] >= 0).all()
    assert len(st) == len(repo.places().dropna(subset=["lat", "lng"]))


def test_follow_ups_and_fee_sentence(orch):
    """이어 묻기는 할 수 있는 것만(5일 넘게 늘리기·이미 버스면 버스 제안 없음), 입장료 문장은 확인된 곳 수와 맞게."""
    assert "3일로 늘려줘" in plan(orch, days=2).follow_ups
    r = plan(orch, days=5, profile=TravelerProfile(mobility="stroller", transport="bus"))
    assert not any("늘려줘" in f or "버스" in f for f in r.follow_ups)
    fee = next(f for f in r.findings if "입장료" in f.text)
    n = len(r.payload["itinerary"]) - fee.numbers["unknown"]
    want = "자료에 없어" if not n else f"확인된 {n}곳은 입장료가 없습니다" if fee.numbers["fee"] == 0 else "확인된 입장료 합계"
    assert want in fee.text


# ── 식당·숙소 배치, 선정 근거, 경로 설명, 길찾기 ─────────────────────────
from jeju_agent.agents import course as course_mod                  # noqa: E402
from jeju_agent.agents.access import place_items   # noqa: E402
from jeju_agent.agents.course import place_extras   # noqa: E402
from jeju_agent.tools import geo                                    # noqa: E402


def _item(item_id, kind, anchor, verdict="적합", name=None, phone="064-000-0000"):
    """가상 PlaceItem(장소 찾기가 판정·정렬해 넘긴 모양)."""
    return {"item_id": item_id, "name": name or item_id, "kind": kind, "verdict": verdict, "anchor_id": anchor,
            "distance_km": 0.5, "phone": phone, "facilities": [{"key": "acc_toilet", "state": "있음", "mark": "✓"}]}


def _stop(pid):
    return {"place_id": pid, "name": pid}


def test_lunch_goes_after_middle_stop_and_lodging_at_day_end():
    plan = [[_stop("C01"), _stop("C02"), _stop("C03")], [_stop("C04"), _stop("C05")]]
    items = [_item("C02-N1", "restaurant", "C02", verdict="부적합"),   # 부적합은 넣지 않는다
             _item("C02-N2", "restaurant", "C02"),
             _item("C03-N1", "lodging", "C03"),
             _item("C04-N1", "restaurant", "C04"),
             _item("C05-N1", "lodging", "C05"),                        # 마지막 날 숙소는 넣지 않는다
             _item("C01-N1", "cafe", "C01")]                           # 카페는 아직 넣지 않는다
    extras, miss = place_extras(plan, items, home=False)
    got = [(x["day"], x["slot"], x["after"], x["item_id"]) for x in extras]
    assert got == [(1, "점심", 2, "C02-N2"), (1, "숙소", 3, "C03-N1"), (2, "점심", 1, "C04-N1")]
    assert miss == {"점심": [], "저녁": [], "숙소": []}


def test_same_shop_once_other_anchor_fallback_and_home_has_no_lodging():
    plan = [[_stop("C01"), _stop("C02")], [_stop("C03")], [_stop("C04")]]
    items = [_item("C01-N1", "restaurant", "C01", name="같은집"), _item("C03-N1", "restaurant", "C03", name="같은집"),
             _item("C02-N1", "lodging", "C02")]
    extras, miss = place_extras(plan, items, home=True)
    assert [(x["day"], x["slot"], x["after"]) for x in extras] == [(1, "점심", 1)]   # 2일차는 같은 가게라 다시 넣지 않음
    assert miss == {"점심": [2, 3], "저녁": [], "숙소": []}                                       # 집에서 매일 출발 → 숙소 없음
    extras, miss = place_extras([[_stop("C09"), _stop("C02")]], [_item("C02-N3", "restaurant", "C02")], home=False)
    assert extras[0]["after"] == 2                                                   # 가운데 장소에 없으면 그날 다른 장소 근처


def _card(nearby):
    return {"place_id": "C01", "name": "코스", "category": "숲", "verdict": "적합", "reasons": [], "facilities": [],
            "source": "s", "data_date": "2021-02-09", "stale": True, "call_script": ["안녕하세요"], "nearby": nearby}


def test_place_items_numbering_and_toilet_rule():
    near = [{"category": "음식점", "name": "가", "distance_km": 0.5, "phone": "",
             "disabled_toilet": "N", "base_date": "2021-12-22", "seq": 3}]
    att, need = place_items([_card(near)], {"C01": (33.4, 126.5)}, TravelerProfile(needs=["acc_toilet"]), access_rules())
    assert (att["item_id"], att["kind"], att["lat"], att["anchor_id"]) == ("C01", "attraction", 33.4, None)
    assert (need["item_id"], need["kind"], need["verdict"], need["phone"], need["lat"]) == ("C01-N3", "restaurant", "부적합", None, None)
    assert need["facilities"][0]["state"] == "없음" and need["anchor_id"] == "C01"
    assert place_items([_card(near)], {}, TravelerProfile(), access_rules())[1]["verdict"] == "적합"   # 화장실이 필요 없으면 적합
    blank = [{**near[0], "disabled_toilet": ""}]
    assert place_items([_card(blank)], {}, TravelerProfile(needs=["acc_toilet"]), access_rules())[1]["verdict"] == "적합"  # 정보 없음 ≠ 없음


def test_access_items_and_course_extras_from_survey(orch):
    """장소 찾기가 payload['items']를 내고, 코스는 주변정보 전체(같은 PlaceItem 규칙)에서 코스 장소 가까운 식당·숙소를 넣는다."""
    prof = TravelerProfile()
    acc = orch._call_agent("access", AnalysisRequest(question="갈 만한 곳", intent=Intent.ACCESS, profile=prof), {})
    items = acc.payload["items"]
    assert items and all(PlaceItem(**i) for i in items)
    assert [i["item_id"] for i in items if i["kind"] == "attraction"] == [c["place_id"] for c in acc.payload["cards"]]
    r = plan(orch, days=2, profile=prof)
    anchors = set(orch.repo.nearby_df()["place_id"])
    assert r.payload["extras"]
    for x in r.payload["extras"]:
        assert PlaceItem(**{k: v for k, v in x.items() if k in PlaceItem.model_fields})
        kto = x["item_id"].startswith("K")   # 관광공사 식당(data/public/kto_bf_restaurant.csv가 있을 때)은 기준 장소가 없다
        assert (kto or x["anchor_id"] in anchors) and x["verdict"] != "부적합" and x["distance_km"] <= 10


def test_dinner_and_last_day_lodging_only_when_asked():
    plan = [[_stop("C01"), _stop("C02")]]
    items = [_item("C02-N1", "restaurant", "C02"), _item("C02-N2", "restaurant", "C02"), _item("C02-N3", "lodging", "C02")]
    extras, _ = place_extras(plan, items, home=False)
    assert [x["slot"] for x in extras] == ["점심"]                       # 1일 코스: 저녁·숙소는 말해야 넣는다
    extras, miss = place_extras(plan, items, home=False, want=["lunch", "dinner", "lodging"])
    assert [(x["slot"], x["item_id"]) for x in extras] == [("점심", "C02-N1"), ("저녁", "C02-N2"), ("숙소", "C02-N3")]
    assert miss == {"점심": [], "저녁": [], "숙소": []}


def test_shop_near_another_survey_course_by_estimated_distance():
    stop = {"place_id": "P001", "name": "관광지", "lat": 33.50, "lng": 126.50}
    near = {"C09": (33.52, 126.50)}                     # 약 2.2km 떨어진 다른 무장애 코스
    items = [{**_item("C09-N1", "restaurant", "C09"), "distance_km": 0.5}]
    extras, _ = place_extras([[stop]], items, home=False, anchor_xy=near, max_km=10)
    assert extras and extras[0]["estimated"] and 2.5 <= extras[0]["distance_km"] <= 3.0
    extras, miss = place_extras([[stop]], items, home=False, anchor_xy=near, max_km=2)
    assert not extras and miss["점심"] == [1]                           # 상한 밖이면 넣지 않는다


def test_wheelchair_toilet_need_explains_why_no_restaurant(orch, monkeypatch):
    """관광공사 식당 자료가 없으면(주변정보 식당은 모두 화장실 없음) 식당을 왜 못 넣었는지 말한다."""
    monkeypatch.setattr(orch.repo, "_kto", pd.DataFrame(columns=kto.COLUMNS))
    mem: dict = {}
    r = orch.ask("휠체어로 서귀포 2일 코스 짜줘. 장애인 화장실 꼭 필요하고 점심 식당도 넣어줘", mem)
    text = r.answer
    assert "모두 장애인 화장실이 없다고 조사돼" in text and "점심 식당 정보가 자료에 없어요" not in text


def test_wheelchair_toilet_need_gets_kto_restaurant_with_toilet(orch):
    """관광공사 식당 자료가 있으면 장애인 화장실이 '있음'인 식당을 점심에 먼저 넣는다(서귀포에는 여러 곳)."""
    if orch.repo.kto_df().empty:
        pytest.skip("data/public/kto_bf_restaurant.csv 없음")
    r = plan(orch, days=2, region="서귀포시", question="점심 식당도 넣어줘",
             profile=TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"]), params={"extras_pick": ["lunch"]})
    lunch = [x for x in r.payload["extras"] if x["slot"] == "점심"]
    assert lunch and all(x["verdict"] != "부적합" for x in lunch)
    assert any(x["item_id"].startswith("K") and x["facilities"][0]["state"] == "있음" for x in lunch)
    c5 = next(t for t in r.evidence if t.id == "C5")
    assert "한국관광공사" in c5.dataset and "출처" in c5.columns


def test_every_stop_has_reasons_and_route_guide(orch):
    r = plan(orch, days=2, profile=TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"]))
    c1 = next(e for e in r.evidence if e.id == "C1")
    assert "선정 근거" in c1.columns and "이동 기준" in c1.columns
    for p in r.payload["itinerary"]:
        assert p["why"] and p["leg_src"] == "추정"                        # 테스트에는 길찾기 키가 없다
        assert set(p["guide"]) == {"route", "parking", "toilet"} and all(p["guide"].values())   # 없으면 '정보 없음'
    assert "extras" in r.payload


def test_road_minutes_replace_estimates_and_respect_caps(settings, repo, tmp_path, monkeypatch):
    o = Orchestrator(dataclasses.replace(settings, kakao_rest_key="dummy"), repo=repo,
                     llm=LLMClient(settings, runtime_dir=tmp_path / "rt"))
    monkeypatch.setattr(course_mod, "road_min", lambda a, b, key: 7)
    r = plan(o, days=1)
    assert {(p["leg_min"], p["leg_src"]) for p in r.payload["itinerary"]} == {(7, "길찾기")}
    monkeypatch.setattr(course_mod, "road_min", lambda a, b, key: None)        # 실패 → 추정치
    assert {p["leg_src"] for p in plan(o, days=1).payload["itinerary"]} == {"추정"}
    monkeypatch.setattr(course_mod, "road_min", lambda a, b, key: 999)         # 실제 길로는 모두 상한 초과 → 넣지 않는다
    assert plan(o, days=1).status == "insufficient"
    assert {p["leg_src"] for p in plan(o, days=1, profile=TravelerProfile(transport="bus")).payload["itinerary"]} == {"추정"}


def test_road_min_parses_kakao_and_backs_off_on_error(monkeypatch):
    import io
    import json as _json
    geo._road_min.cache_clear()
    monkeypatch.setattr(geo, "_down_until", 0.0)
    body = {"routes": [{"result_code": 0, "summary": {"duration": 1500, "distance": 20000}}]}
    monkeypatch.setattr(geo.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(_json.dumps(body).encode()))
    assert geo.road_min((33.5, 126.5), (33.3, 126.6), "k") == 25
    assert geo.road_min((33.5, 126.5), (33.3, 126.6), "") is None              # 키 없음 → 부르지 않음

    def boom(req, timeout):
        raise OSError("network down")
    monkeypatch.setattr(geo.urllib.request, "urlopen", boom)
    assert geo.road_min((33.1, 126.1), (33.2, 126.2), "k") is None
    assert geo._down_until > 0                                                 # 잠시 쉬어서 구간마다 시간 초과를 기다리지 않는다
    geo._road_min.cache_clear()


def test_screen_choice_of_meals_and_lodging():
    """화면에서 고른 식당·숙소만 넣는다(None = 기본 점심·숙소). 말로 부탁한 것(want)은 고른 것과 상관없이 넣는다."""
    plan = [[_stop("C01"), _stop("C02")], [_stop("C03")]]
    items = [_item("C01-N1", "restaurant", "C01"), _item("C02-N1", "restaurant", "C02"), _item("C02-N2", "lodging", "C02"),
             _item("C03-N1", "restaurant", "C03")]
    slots = lambda **kw: [(x["day"], x["slot"]) for x in place_extras(plan, items, home=False, **kw)[0]]   # noqa: E731
    assert slots() == [(1, "점심"), (1, "숙소"), (2, "점심")]
    assert slots(choice=[]) == []
    assert slots(choice=["lodging"]) == [(1, "숙소")]
    assert slots(choice=["lunch", "dinner"]) == [(1, "점심"), (1, "저녁"), (2, "점심")]
    assert slots(choice=[], want=["dinner"]) == [(1, "저녁"), (2, "저녁")]


def test_form_extras_choice_survives_chat_edits(orch):
    """폼에서 식당·숙소를 끄면 코스에 없고, 대화로 코스를 고쳐도 다시 생기지 않는다."""
    mem: dict = {}
    form = {"profile": TravelerProfile(), "days": 2, "region": "서귀포시", "intent": "course", "start_date": "2026-11-18",
            "extras": []}
    r = orch.execute(orch.plan_form("서귀포시 2일 여행 코스", form), mem)
    assert r.plan.request.month == 11 and not r.results[-1].payload.get("extras")
    first = r.results[-1].payload["itinerary"][0]["name"]
    r2 = orch.ask(f"{first} 빼줘", mem)
    assert r2.plan.request.params.get("extras_pick") == [] and not r2.results[-1].payload.get("extras")
    assert r2.plan.request.start_date == "2026-11-18"


def test_road_path_on_map_and_bus_stops(settings, repo, tmp_path, monkeypatch):
    """자가용: 길찾기로 잰 구간에 도로 좌표(path)를 붙이고(하루 첫 이동 제외), 실패한 구간은 path 없음 → 지도는 직선.
    버스: path 없이 장소마다 가장 가까운 정류소(stop)."""
    o = Orchestrator(dataclasses.replace(settings, kakao_rest_key="dummy"), repo=repo,
                     llm=LLMClient(settings, runtime_dir=tmp_path / "rt"))
    monkeypatch.setattr(course_mod, "road_min", lambda a, b, key: 7)
    monkeypatch.setattr(course_mod, "road_path", lambda a, b, key: [a, ((a[0] + b[0]) / 2, a[1]), b])
    it = plan(o, days=2).payload["itinerary"]
    firsts = [p for p in it if p["order"] == 1]
    assert all("path" not in p for p in firsts) and all(len(p["path"]) == 3 for p in it if p["order"] > 1)
    bus = plan(o, days=2, profile=TravelerProfile(transport="bus")).payload["itinerary"]
    assert all("path" not in p for p in bus) and all({"name", "m", "lat", "lng"} <= set(p["stop"]) for p in bus)


def test_road_path_parses_vertexes(monkeypatch):
    import io
    import json as _json
    geo._road_min.cache_clear()
    monkeypatch.setattr(geo, "_down_until", 0.0)
    body = {"routes": [{"result_code": 0, "summary": {"duration": 600},
                        "sections": [{"roads": [{"vertexes": [126.5, 33.5, 126.55, 33.45]}, {"vertexes": [126.6, 33.4]}]}]}]}
    monkeypatch.setattr(geo.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(_json.dumps(body).encode()))
    assert geo.road_min((33.5, 126.5), (33.4, 126.6), "k") == 10
    assert geo.road_path((33.5, 126.5), (33.4, 126.6), "k") == [(33.5, 126.5), (33.45, 126.55), (33.4, 126.6)]
    assert geo.road_path((33.5, 126.5), (33.4, 126.6), "") is None
    assert len(geo._thin([(i, i) for i in range(1000)], 200)) == 200
    geo._road_min.cache_clear()


# ── 코스 효율: 방문 순서 다듬기 · 숙소에서 다음 날 출발 · 덜 돌아가는 점심 · 식당 이동을 하루 시간에 ──
def _cands(rows):
    return pd.DataFrame([{"verdict": "적합", "indoor": 0, **r} for r in rows])


def test_reorder_shortens_day_and_lodging_sets_next_start():
    rules = access_rules()
    # A는 멀고 점수 높음, B는 A 가는 길의 가까운 곳 → 고르는 순서는 A, B지만 다듬으면 B, A
    c = _cands([{"place_id": "A", "name": "A", "score": 100, "lat": 33.50, "lng": 126.70},
                {"place_id": "B", "name": "B", "score": 10, "lat": 33.50, "lng": 126.58}])
    raw, _ = course_mod.schedule(c, 1, 2, 60, 480, 60, 0, "car", rules, (33.50, 126.50), reorder=False)
    tidy, _ = course_mod.schedule(c, 1, 2, 60, 480, 60, 0, "car", rules, (33.50, 126.50))
    assert [p["name"] for p in raw[0]] == ["A", "B"] and [p["name"] for p in tidy[0]] == ["B", "A"]
    assert sum(p["leg_min"] for p in tidy[0]) < sum(p["leg_min"] for p in raw[0])
    # 1일차가 끝날 때 숙소 좌표를 돌려주면 2일차 첫 이동은 그 숙소에서 잰다
    c2 = _cands([{"place_id": "X", "name": "X", "score": 50, "lat": 33.30, "lng": 126.30},
                 {"place_id": "Y", "name": "Y", "score": 40, "lat": 33.25, "lng": 126.30}])
    hotel = (33.25, 126.31)
    p_lodge, _ = course_mod.schedule(c2, 2, 1, 60, 480, 120, 0, "car", rules, (33.30, 126.31),
                                     day_end=lambda d, day: hotel if d == 1 else None)
    p_plain, _ = course_mod.schedule(c2, 2, 1, 60, 480, 120, 0, "car", rules, (33.30, 126.31))
    assert p_lodge[1][0]["leg_min"] < p_plain[1][0]["leg_min"]


def test_lunch_prefers_least_detour_when_shops_have_coordinates():
    day = [{"place_id": "S1", "name": "S1", "lat": 33.50, "lng": 126.50},
           {"place_id": "S2", "name": "S2", "lat": 33.50, "lng": 126.60}]
    on_way = {**_item("R1", "restaurant", "S1", name="길 위 식당"), "lat": 33.50, "lng": 126.55}
    back = {**_item("R2", "restaurant", "S1", name="뒤쪽 식당", phone="064-111-1111"), "lat": 33.50, "lng": 126.47}
    extras, _ = place_extras([day], [back, on_way], home=True, max_km=10)
    lunch = next(x for x in extras if x["slot"] == "점심")
    assert lunch["name"] == "길 위 식당" and not lunch["estimated"] and lunch["detour_km"] < 0.5
    # 좌표 있는 숙소는 nearest_lodging이 고르고, place_extras는 그 숙소를 그대로 쓴다
    inn = {**_item("H1", "lodging", "S2", name="숙소"), "lat": 33.51, "lng": 126.60}
    got = course_mod.nearest_lodging([inn], day[-1], set(), 5)
    extras, _ = place_extras([day, day], [inn], home=False, choice=["lodging"], lodging_fixed={1: got})
    assert [(x["day"], x["slot"], x["name"]) for x in extras] == [(1, "숙소", "숙소")]


def test_meal_detour_trims_a_day_but_never_below_two(orch, monkeypatch):
    """식당까지 크게 돌아가 하루 시간을 넘으면 그날 1곳만 줄이고(한 번만 다시 짬), 하루 2곳 아래로는 줄이지 않는다."""
    import copy
    rules = copy.deepcopy(access_rules())
    monkeypatch.setattr(course_mod, "access_rules", lambda: rules)

    def far_pool(ctx, profile, rules, with_kto=True):   # 모든 코스 장소에서 9km 떨어진 식당(좌표 없음 → 왕복 18km 추정)
        return ([{**_item(f"R{i}", "restaurant", "Z", name=f"먼 식당{i}", phone=f"064-{i:03d}"), "distance_km": 9.0}
                 for i in range(10)], {"Z": (33.45, 126.55)})
    monkeypatch.setattr(course_mod, "extra_pool", far_pool)
    monkeypatch.setattr(course_mod, "extra_km", lambda i, s, axy: (9.0, True))

    def run(day_minutes):
        rules["course"]["pace"]["보통"] = {"per_day": 3, "visit_min": 60, "day_minutes": day_minutes}
        r = plan(orch, days=2, profile=TravelerProfile(), region="제주시", params={"extras_pick": ["lunch"]})
        return r, [len([p for p in r.payload["itinerary"] if p["day"] == d]) for d in (1, 2)]

    roomy, n_roomy = run(480)
    assert n_roomy == [3, 3] and "rule:meal_time" not in [t.action for t in roomy.trace]   # 여유가 있으면 줄이지 않는다
    tight, n_tight = run(200)        # 3곳·관람 180분 + 이동 + 점심 우회 35분 → 넘침
    assert "rule:meal_time" in [t.action for t in tight.trace]
    assert n_tight == [2, 2] and min(n_tight) >= course_mod.MIN_PER_DAY
    assert all(x["meal_min"] > 0 for x in tight.payload["extras"] if x["slot"] == "점심")
    _, n_min = run(100)              # 2곳만 들어가는 날은 넘쳐도 더 줄이지 않는다
    assert min(n_min) >= 1 and all(n <= 2 for n in n_min)


def test_shop_coordinates_are_not_stored(repo):
    """카카오 로컬 API 결과는 저장 금지(운영정책): 저장소에 가게 좌표 파일·만드는 스크립트가 없고, 주변정보 표에도 좌표가 없다."""
    root = Path(__file__).resolve().parents[1]
    assert not (root / "data" / "public" / "nearby_xy.csv").exists() and not (root / "scripts" / "geocode_nearby.py").exists()
    nd = Repository("public").nearby_df()
    assert len(nd) and nd["lat"].isna().all()


def test_live_xy_fills_only_needed_shops_and_forgets(monkeypatch):
    calls: list[str] = []

    def fetch(addr, key):
        calls.append(addr)
        return {"제주시 가로 1": (33.50, 126.53), "제주시 먼 길 9": (33.30, 126.20), "서울 어딘가": (37.5, 127.0)}.get(addr)

    items = [{"name": "가", "kind": "restaurant", "address": "제주시 가로 1", "anchor_id": "A", "distance_km": 0.5, "lat": None},
             {"name": "나", "kind": "restaurant", "address": "제주시 먼 길 9", "anchor_id": "A", "distance_km": 0.5, "lat": None},
             {"name": "다", "kind": "restaurant", "address": "서울 어딘가", "anchor_id": "A", "distance_km": 0.5, "lat": None},
             {"name": "라", "kind": "lodging", "address": "제주시 가로 1", "anchor_id": "A", "distance_km": 0.5, "lat": None}]
    live = geo.LiveXY("key", fetch=fetch)
    live.fill(items, lambda i: i["kind"] == "restaurant", {"A": (33.501, 126.531)})
    assert sorted(calls) == ["서울 어딘가", "제주시 가로 1", "제주시 먼 길 9"]          # 필요한 가게만, 같은 주소는 한 번
    assert items[0]["lat"] == 33.50 and items[0]["xy_from"].startswith("카카오")
    assert items[1]["lat"] is None          # 기준 코스에서 조사 거리 + 2km 넘게 떨어짐 → 버림
    assert items[2]["lat"] is None          # 제주 밖 → 버림
    assert items[3]["lat"] is None          # 이번에 필요 없던 숙소는 찾지 않음
    live.fill(items, lambda i: True, {"A": (33.501, 126.531)})
    assert len(calls) == 3 and items[3]["lat"] == 33.50                                 # 이번 코스 안에서는 다시 부르지 않음
    assert geo.LiveXY("key", fetch=fetch)._got == {}                                   # 새 코스는 빈 상태(저장 안 함)


def test_live_xy_stops_after_error():
    n = {"calls": 0}

    def boom(addr, key):
        n["calls"] += 1
        raise OSError("401")

    live = geo.LiveXY("key", fetch=boom, workers=1)
    items = [{"name": str(k), "kind": "restaurant", "address": f"주소 {k}", "lat": None} for k in range(5)]
    live.fill(items, lambda i: True, {})
    assert live.down and n["calls"] == 1 and all(i["lat"] is None for i in items)
    live.fill(items, lambda i: True, {})
    assert n["calls"] == 1


def test_course_uses_live_shop_coordinates_with_key(settings, repo, tmp_path, monkeypatch):
    """키가 있으면 코스 근처 가게만 실시간으로 좌표를 찾아 쓰고(기록에 횟수만), 키가 없으면 추정 거리."""
    near = Repository("public").nearby_df()
    anchors = Repository("public").get("places").df.set_index("place_id")
    xy = {}
    for _, r in near.iterrows():   # 가짜 카카오: 기준 코스 좌표 근처로 돌려준다
        a = anchors.loc[r["place_id"]] if r["place_id"] in anchors.index else None
        if a is not None and pd.notna(a["lat"]):
            xy[r["address"]] = (float(a["lat"]) + 0.002, float(a["lng"]) + 0.002)
    seen: list[str] = []
    monkeypatch.setattr(geo, "_kakao_address", lambda addr, key: (seen.append(addr), xy.get(addr))[1])
    monkeypatch.setattr(geo, "_road", lambda a, b, key: None)   # 길찾기는 부르지 않음(추정치)
    s = dataclasses.replace(settings, kakao_rest_key="dummy")
    o = Orchestrator(s, repo=repo, llm=LLMClient(s, runtime_dir=tmp_path))
    req = AnalysisRequest(question="코스", intent=Intent.COURSE, region="서귀포시", days=2,
                          profile=TravelerProfile(mobility="wheelchair_manual"), params={"extras_pick": ["lunch", "lodging"]})
    r = o._call_agent("course", req, {})
    hub = [x for x in r.payload["extras"] if not x["item_id"].startswith("K")]
    assert seen and len(seen) < len(near)                                      # 코스 근처 후보만
    assert any(t.action == "tool:kakao_address" and "저장하지 않음" in t.detail for t in r.trace)
    assert all(x.get("xy_from") or x.get("estimated") for x in hub)
    off = Orchestrator(settings, repo=repo, llm=LLMClient(settings, runtime_dir=tmp_path))
    r2 = off._call_agent("course", req, {})
    assert not any(t.action == "tool:kakao_address" for t in r2.trace)


def test_route_cache_is_memory_only_and_expires(monkeypatch):
    n = {"calls": 0}

    @geo._ttl_cache(10, 2)
    def f(x):
        n["calls"] += 1
        return x * 2

    now = [1000.0]
    monkeypatch.setattr(geo.time, "time", lambda: now[0])
    assert f(1) == 2 and f(1) == 2 and n["calls"] == 1
    now[0] += 11
    assert f(1) == 2 and n["calls"] == 2                       # 시간이 지나면 다시 묻는다
    f(2), f(3)
    f(1)
    assert n["calls"] == 5                                     # 넘치면 오래된 것부터 버린다
    assert geo.ROUTE_TTL_S <= 24 * 3600

def test_indoor_wish_adds_indoor_caution_places(orch):
    """웹 실측(11/19): '비 오면 걱정, 실내 위주로 제주시 2일' → 실내 '적합'이 1곳뿐이라 바닷가 실외 코스가 나왔음.
    실내 적합이 모자라면 실내 '주의' 장소(박물관·미술관)를 후보에 더해 다시 짠다. '넣었다'고 거짓 문장을 쓰지 않는다."""
    from jeju_agent.contracts import AnalysisRequest, Intent, TravelerProfile
    prof = TravelerProfile(mobility="senior", needs=["indoor_pref"], interests=["museum"])
    r = orch._call_agent("course", AnalysisRequest(question="실내 위주 코스", intent=Intent.COURSE, region="제주시", days=2,
                                                   profile=prof), {})
    assert "rule:include_caution_indoor" in [t.action for t in r.trace]
    assert not r.payload["indoor_short_days"] and all(p["verdict"] != "부적합" for p in r.payload["itinerary"])
    texts = [f.text for f in r.findings]
    assert any("실내 '적합' 장소가 모자라" in t for t in texts)
    assert not any("넣으려 했습니다" in t for t in texts) or r.payload["indoor_short_days"]
