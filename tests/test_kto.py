"""한국관광공사 무장애 식당(data/kto.py) → 코스 식당 후보(kto_items·merge_kto·shop_rank)."""
import pandas as pd

from jeju_agent.agents.access import kto_items
from jeju_agent.agents.course import merge_kto, place_extras, shop_basis
from jeju_agent.config import access_rules
from jeju_agent.contracts import PlaceItem, TravelerProfile
from jeju_agent.data import kto


def _raw(**kw):
    base = {"contentid": "1", "title": "가게", "addr1": "제주특별자치도 제주시 어딘가로 1", "addr2": "", "mapx": "126.50",
            "mapy": "33.50", "lclsSystm2": "FD01", "restroom": "", "exit": "", "route": "", "parking": "",
            "modifiedtime": "20261116171245", "fetched": "2026-11-18"}
    return {**base, **kw}


def test_parse_states_from_sentences():
    df = kto.parse(pd.DataFrame([
        _raw(contentid="1", title="화장실", restroom="장애인 전용 화장실 있음", exit="주출입구는 턱이 없어 휠체어 접근 가능함(수동문)"),
        _raw(contentid="2", title="좁은 곳", exit="문의 형태 : 미닫이문출입통로가 좁아 휠체어, 전동 스쿠터 진입 주의.경사로 있음."),
        _raw(contentid="3", title="빈칸"),
        _raw(contentid="4", title="카페", lclsSystm2="FD05", route="출입구까지 경사로가 설치되어 있음_무장애 편의시설"),
        _raw(contentid="5", title="좌표 없음", mapx="", mapy=""),
        _raw(contentid="6", title="없다고 적힘", restroom="장애인 화장실 없음"),
    ]))
    s = df.set_index("name")
    assert len(df) == 5                                              # 좌표 없는 줄은 뺀다
    assert (s.loc["화장실", "toilet"], s.loc["화장실", "entry"]) == ("있음", "가능")
    assert s.loc["좁은 곳", "entry"] == "주의"                         # '경사로 있음'이 함께 있어도 어렵다는 말이 먼저
    assert (s.loc["빈칸", "toilet"], s.loc["빈칸", "entry"]) == ("정보 없음", "정보 없음")   # 빈칸 = 모름(없음 아님)
    assert s.loc["카페", "kind"] == "cafe" and s.loc["카페", "entry_ev"] == "출입구까지 경사로가 설치되어 있음"
    assert s.loc["없다고 적힘", "toilet"] == "없음" and s.loc["화장실", "data_date"] == "2026-11-16"
    assert kto.clean("가<br/>나") == "가 나" and kto.norm_name("금돈가 제주본점") == kto.norm_name("금돈가")


def test_kto_items_keep_only_known_access_and_judge():
    df = kto.parse(pd.DataFrame([
        _raw(contentid="1", title="화장실만", restroom="장애인 화장실 있음"),
        _raw(contentid="2", title="출입 쉬움", exit="출입통로가 넓어 휠체어, 전동 스쿠터 진입 쉬움."),
        _raw(contentid="3", title="출입 주의", exit="진입로에 턱이 있어 휠체어, 전동 스쿠터 진입 어려움."),
        _raw(contentid="4", title="정보 없음"),
        _raw(contentid="5", title="화장실 없음", restroom="장애인 화장실 없음", exit="턱 없음"),
    ]))
    wc = TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet"])
    items = {i["name"]: i for i in kto_items(df, wc, access_rules())}
    assert set(items) == {"화장실만", "출입 쉬움", "출입 주의", "화장실 없음"}   # 출입·화장실 모두 모르는 곳은 뺀다
    assert all(PlaceItem(**i) for i in items.values())
    assert items["화장실만"]["verdict"] == "적합" and items["화장실만"]["facilities"][0]["state"] == "있음"
    assert items["출입 쉬움"]["verdict"] == "적합" and "장애인 화장실이 있나요?" in items["출입 쉬움"]["call_script"]
    assert items["출입 주의"]["verdict"] == "주의" and items["화장실 없음"]["verdict"] == "부적합"
    assert items["출입 쉬움"]["source"].startswith(kto.SOURCE) and items["출입 쉬움"]["item_id"] == "K2"
    plain = {i["name"]: i for i in kto_items(df, TravelerProfile(), access_rules())}
    assert plain["출입 주의"]["verdict"] == "적합" and plain["화장실 없음"]["verdict"] == "적합"   # 조건이 없으면 막지 않는다


def _hub(name, toilet="N", lat=33.5, lng=126.5):
    return {"item_id": "C01-N1", "name": name, "kind": "restaurant", "verdict": "적합", "anchor_id": "C01", "distance_km": 0.5,
            "phone": "064-000-0000", "lat": lat, "lng": lng, "source": "제주데이터허브 무장애여행코스 주변정보 (2021-12-22 조사)",
            "facilities": [{"key": "acc_toilet", "state": {"Y": "있음", "N": "없음"}[toilet], "mark": "✗"}]}


def test_merge_same_shop_keeps_kto_and_survey_toilet():
    df = kto.parse(pd.DataFrame([_raw(contentid="9", title="금돈가 제주본점", exit="주출입구는 턱이 없어 휠체어 접근 가능함")]))
    prof = TravelerProfile(needs=["acc_toilet"])
    merged = merge_kto([_hub("금돈가"), _hub("다른 집", lat=33.4)], kto_items(df, prof, access_rules()), prof)
    names = [i["name"] for i in merged]
    assert names == ["다른 집", "금돈가 제주본점"]                    # 같은 가게는 관광공사 하나로
    k = merged[1]
    assert k["phone"] == "064-000-0000"                              # 전화번호는 주변정보에서
    assert k["facilities"][0]["state"] == "없음" and k["verdict"] == "부적합"   # 관광공사 빈칸 < 2021 조사 '없음'
    far = merge_kto([_hub("금돈가", lat=33.3)], kto_items(df, prof, access_rules()), prof)
    assert len(far) == 2                                             # 이름이 같아도 300m 넘게 떨어지면 다른 가게


def test_lunch_prefers_toilet_when_needed_even_if_farther():
    day = [{"place_id": "S1", "name": "S1", "lat": 33.50, "lng": 126.50}]
    near = {"item_id": "K1", "name": "가까운 식당", "kind": "restaurant", "verdict": "적합", "phone": None, "lat": 33.50, "lng": 126.51,
            "facilities": [{"key": "acc_toilet", "state": "정보 없음", "mark": "?"}], "source": kto.SOURCE, "data_date": "2026-11-16"}
    toilet = {**near, "item_id": "K2", "name": "화장실 식당", "lng": 126.54,
              "facilities": [{"key": "acc_toilet", "state": "있음", "mark": "✓"}]}
    hard = {**near, "item_id": "K3", "name": "주의 식당", "lng": 126.501, "verdict": "주의"}
    got, _ = place_extras([day], [near, toilet, hard], home=True, max_km=6, need_toilet=True)
    assert got[0]["name"] == "화장실 식당"
    got, _ = place_extras([day], [near, toilet, hard], home=True, max_km=6)
    assert got[0]["name"] == "가까운 식당"                              # 화장실 조건이 없으면 가까운 곳, '주의'는 나중
    got, _ = place_extras([day], [near, toilet, hard], home=True, max_km=2, need_toilet=True)
    assert got[0]["name"] == "가까운 식당"                              # 화장실 식당이 거리 상한 밖이면 다음 순위
    assert shop_basis(got[0]) == "한국관광공사(2026-11 수정)"


def test_real_file_counts_if_present():
    from jeju_agent.data import Repository
    df = Repository("public").kto_df()
    if df.empty:
        return
    rest = df[df["kind"] == "restaurant"]
    assert (df["toilet"] == "없음").sum() == 0 and len(df) >= 70
    keep = rest[(rest["toilet"] == "있음") | (rest["entry"] != "정보 없음")]
    assert len(keep) >= 50 and (keep["toilet"] == "있음").sum() >= 5


def test_toilet_first_has_a_detour_cap():
    """화장실 있는 식당이 너무 멀면(상한 넘게 더 돌아가면) 가까운 식당을 넣는다."""
    day = [{"place_id": "S1", "name": "S1", "lat": 33.50, "lng": 126.50}]
    near = {"item_id": "K1", "name": "가까운 식당", "kind": "restaurant", "verdict": "적합", "phone": None, "lat": 33.50, "lng": 126.51,
            "facilities": [{"key": "acc_toilet", "state": "정보 없음", "mark": "?"}], "source": kto.SOURCE}
    toilet = {**near, "item_id": "K2", "name": "화장실 식당", "lng": 126.58,      # 약 7.4km(점심 왕복 약 15km)
              "facilities": [{"key": "acc_toilet", "state": "있음", "mark": "✓"}]}
    got, _ = place_extras([day], [near, toilet], home=True, max_km=10, need_toilet=True)
    assert got[0]["name"] == "화장실 식당"                                # 상한이 없으면 화장실 먼저
    got, _ = place_extras([day], [near, toilet], home=True, max_km=10, need_toilet=True, toilet_slack_km=5)
    assert got[0]["name"] == "가까운 식당"                                # 14km 넘게 더 돌아가 상한(5km) 초과
    got, _ = place_extras([day], [near, toilet], home=True, max_km=10, need_toilet=True, toilet_slack_km=20)
    assert got[0]["name"] == "화장실 식당"
    got, _ = place_extras([day], [toilet], home=True, max_km=10, need_toilet=True, toilet_slack_km=5)
    assert got[0]["name"] == "화장실 식당"                                # 다른 식당이 없으면 그대로
