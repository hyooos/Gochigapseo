"""화면 말(friendly) — 낱말 바꾸기, 여러 개 고른 조건 합치기."""
from jeju_agent.contracts import TravelerProfile
from jeju_agent.friendly import merge_conditions, search_links, soften


def test_soften_words_but_keep_quotes_and_place_names():
    assert soften("장애인 화장실 확인 [근거표 C2]") == "휠체어 화장실 확인 [자료표 C2]"
    assert "장애인" not in soften("장애인 주차구역, 장애인전용주차 2면")
    assert soften("원문: “장애인화장실(남녀 구분)”") == "원문: “장애인화장실(남녀 구분)”"   # 조사 원문은 그대로
    assert soften("자연휴양림 무장애길") == "자연휴양림 무장애길"                       # 장소 이름은 그대로


def test_merge_conditions_takes_strictest_and_keeps_the_rest():
    got = merge_conditions(["senior", "wheelchair_power"], ["low", "blind"], [], ["acc_toilet"], ["intellectual"])
    assert got == {"mobility": "wheelchair_power", "vision": "blind", "hearing": "none", "cognition": "intellectual",
                   "needs": ["acc_toilet", "rest_often"]}
    none = merge_conditions([], [], [], [])
    assert TravelerProfile(**none) == TravelerProfile()                  # 아무것도 안 고르면 기본 조건


def test_search_links_drop_dev_marker():
    assert "(가상)" not in search_links("노을 해변(가상)")["kakao"]


def test_none_button_is_exclusive_and_default():
    from jeju_agent.friendly import exclusive_none as ex
    assert ex(["none", "low"], ["none"]) == ["low"]            # 다른 것을 누르면 '해당 없음'이 꺼진다
    assert ex(["low", "none"], ["low"]) == ["none"]            # '해당 없음'을 누르면 나머지가 꺼진다
    assert ex([], ["low"]) == ["none"]                        # 모두 끄면 '해당 없음'
    assert ex(["low", "blind"], ["low"]) == ["low", "blind"]
    assert merge_conditions(["none"], ["none"], ["none"], ["none"], ["none"]) == merge_conditions([], [], [], [])
