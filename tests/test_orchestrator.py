"""질문 해석(규칙): 이용자 조건·일수·달·지역·의도, 이어 묻기, 화면 폼."""
import pytest

from jeju_agent.contracts import Intent, TravelerProfile
from jeju_agent.orchestrator import parse_days, parse_month, parse_profile


@pytest.mark.parametrize("q,want", [
    ("휠체어 타는 어머니와", {"mobility": "wheelchair_manual"}),
    ("전동휠체어로 다녀요", {"mobility": "wheelchair_power"}),
    ("목발을 짚어서 오래 못 걸어요", {"mobility": "walker"}),
    ("80대 부모님 모시고", {"mobility": "senior"}),
    ("유모차 끌고 가요", {"mobility": "stroller"}),
    ("시각장애가 있어 안내견과 함께", {"vision": "blind"}),
    ("보청기를 써요", {"hearing": "hard"}),
    ("장애인 화장실 꼭 필요하고 비 오는 날도 괜찮은 곳", {"needs": ["acc_toilet", "indoor_pref"]}),
    ("버스로 다닐 거예요", {"transport": "bus"}),
    ("4명이 가요 1인 2만원 이내", {"companions": 4}),
])
def test_parse_profile(q, want):
    got, _ = parse_profile(q)
    for k, v in want.items():
        assert got[k] == v, k


@pytest.mark.parametrize("q,days", [("2박3일", 3), ("1박 코스", 2), ("하루 코스", 1), ("3일 일정", 3), ("10월 3일 여행", None),
                                     ("이틀 동안", 2)])
def test_parse_days(q, days):
    assert parse_days(q) == days


def test_parse_month():
    assert parse_month("3월에 가요") == 3 and parse_month("12월") == 12 and parse_month("2박3일") is None


@pytest.mark.parametrize("q,intent", [
    ("휠체어로 갈 만한 곳 추천해줘", Intent.ACCESS),
    ("유모차로 2박3일 코스 짜줘", Intent.COURSE),
    ("읍면동별 장애인 화장실 현황", Intent.STATUS),
    ("사업계획서에 쓸 무장애 관광지 통계", Intent.STATUS),
    ("성산일출봉 지금 붐벼? 실시간으로", Intent.UNSUPPORTED),
    ("애월 맛집 알려줘", Intent.NEARBY),
    ("숙소 추천", Intent.NEARBY),
    ("이호테우해변 근처 카페", Intent.NEARBY),
    ("2일 코스 짜줘 숙소도", Intent.COURSE),        # 코스 요청이 먼저: 코스가 숙소를 함께 넣는다
    ("휠체어 가능한 숙소 예약해줘", Intent.UNSUPPORTED),   # 예약은 여전히 못 한다
    ("애월에 숙소 잡았어 갈 만한 곳", Intent.ACCESS),   # 출발지 표현의 '숙소'는 근처 질문이 아니다
    ("보고서로 정리해줘", Intent.REPORT),
    ("안녕", Intent.UNSUPPORTED),
])
def test_intents(orch, q, intent):
    assert orch.plan(q).request.intent == intent


@pytest.mark.parametrize("q,region", [("애월에서", "애월읍"), ("성산 쪽", "성산읍"), ("서귀포에", "서귀포시"), ("중문 관광단지", "중문동"),
                                      ("가운데 중앙에 있는", None), ("제주 어디든", None)])
def test_regions(orch, q, region):
    assert orch.plan(q + " 갈 만한 곳").request.region == region


def test_follow_up_keeps_profile_and_places(orch):
    mem: dict = {}
    first = orch.ask("휠체어로 서귀포 갈 만한 곳. 장애인 화장실 필요", mem)
    shown = first.results[-1].payload["shown_ids"]
    plan = orch.plan("그중 이 곳들로 2일 코스 짜줘 3월", mem)
    req = plan.request
    assert req.intent == Intent.COURSE and req.days == 2 and req.month == 3 and req.region == "서귀포시"
    assert req.profile.mobility == "wheelchair_manual" and "acc_toilet" in req.profile.needs
    assert req.place_ids == shown
    orch.execute(plan, mem)
    plan2 = orch.plan("다시 버스로 다니면?", mem)
    assert plan2.request.intent == Intent.COURSE and plan2.request.days == 2
    assert plan2.request.profile.transport == "bus" and plan2.request.profile.mobility == "wheelchair_manual"


@pytest.mark.parametrize("follow", ["이 곳들로 2일 코스 짜줘", "실내 장소 위주로 다시 보여줘", "3일로 늘려줘", "다시 버스로 다니는 코스로 짜줘"])
def test_suggested_follow_ups_are_understood(orch, follow):
    """화면에 띄우는 '이어서 물어보기' 문장이 직전 조건을 이어받아 해석된다."""
    mem: dict = {}
    orch.ask("휠체어로 서귀포 2일 코스 짜줘", mem)
    req = orch.plan(follow, mem).request
    assert req.intent in (Intent.ACCESS, Intent.COURSE) and req.profile.mobility == "wheelchair_manual"
    assert req.region == "서귀포시"


def test_form_plan_skips_parsing(orch):
    prof = TravelerProfile(mobility="walker", needs=["rest_often"])
    p = orch.plan_form("서귀포시 2일 여행 코스", {"intent": "course", "profile": prof, "region": "제주 전체", "days": 2, "month": 5})
    assert p.parser == "form" and p.request.region is None and p.request.days == 2 and p.request.profile == prof
    assert "지팡이·보행보조기" in p.understood


def test_unsupported_answer_is_honest(orch):
    r = orch.ask("오늘 성산일출봉 실시간 혼잡도 알려줘")
    assert r.results == [] and "근거 있게 답할 수 없는" in r.answer and "직접 확인" in r.answer
