"""부정 표현: '장애인 화장실은 필요 없어'·'휠체어 안 타요'를 조건으로 넣지 않고, 이어지는 대화에서는 그 조건을 뺀다."""
import pytest

from jeju_agent.contracts import TravelerProfile
from jeju_agent.orchestrator import drop_conditions, parse_dropped, parse_profile


@pytest.mark.parametrize("q, want", [
    ("휠체어로 서귀포 2일 코스 짜줘. 장애인 화장실은 필요 없어", {"mobility": "wheelchair_manual"}),
    ("장애인 화장실 없어도 돼요", {}),
    ("휠체어 안 타요. 서귀포 코스 짜줘", {}),
    ("휠체어는 필요없고 지팡이 짚어요", {"mobility": "walker"}),
    ("휠체어 타지 않아요 지팡이만 써요", {"mobility": "walker"}),
    ("주차장 필요 없어. 서귀포 코스 짜줘", {}),
    ("장애인 화장실 필요 없고 주차 꼭 필요해", {"needs": ["acc_parking"]}),
    ("화장실 필요하고 주차는 필요 없어", {"needs": ["acc_toilet"]}),           # 마디마다 따로
    ("전동휠체어 말고 수동 휠체어예요", {"mobility": "wheelchair_manual"}),
    ("바다 말고 숲 위주로", {"interests": ["forest"]}),
    ("버스 안 타고 택시로 다닐게요", {"transport": "taxi"}),
    ("실내 아니어도 돼요", {}),
])
def test_negated_words_are_not_conditions(q, want):
    assert parse_profile(q)[0] == want


@pytest.mark.parametrize("q, need", [
    ("휠체어로 코스 짜줘. 장애인 화장실 꼭 있어야 해", True),
    ("장애인 화장실 없으면 안 돼요", True),          # '없으면 안 돼' = 필요하다
    ("화장실 없는 곳은 빼줘", True),                 # 없는 곳을 빼 달라 = 필요하다
    ("엄마가 휠체어 타시고 화장실이 꼭 필요해요", True),
    ("휠체어 화장실 필요해요", True),
])
def test_need_statements_still_found(q, need):
    assert ("acc_toilet" in parse_profile(q)[0].get("needs", [])) is need
    assert "needs" not in parse_dropped(q)


def test_dropped_and_drop_conditions():
    d = parse_dropped("휠체어 안 타요. 장애인 화장실도 필요 없어요")
    assert set(d["mobility"]) == {"wheelchair_manual", "wheelchair_power"} and d["needs"] == ["acc_toilet"]
    p = TravelerProfile(mobility="wheelchair_power", needs=["acc_toilet", "acc_parking"])
    q = drop_conditions(p, d)
    assert q.mobility == "none" and q.needs == ["acc_parking"]


def test_follow_up_drops_condition_from_course(orch):
    mem: dict = {}
    r1 = orch.ask("휠체어로 서귀포 2일 코스 짜줘. 장애인 화장실 꼭 필요해", mem)
    assert r1.plan.request.profile.needs == ["acc_toilet"]
    pl = orch.plan("장애인 화장실은 이제 필요 없어", mem, ask_profile=True)
    req = pl.request
    assert req.intent.value == "course" and req.days == 2 and req.region == r1.plan.request.region
    assert req.profile.mobility == "wheelchair_manual" and req.profile.needs == []
    assert "뺀 조건: 장애인 화장실" in pl.understood
    r2 = orch.execute(pl, mem)
    assert r2.results and r2.results[-1].payload.get("itinerary")


def test_follow_up_switches_mobility(orch):
    mem: dict = {}
    orch.ask("휠체어로 제주시 장소 찾아줘", mem)
    pl = orch.plan("아 휠체어 안 타요. 지팡이 짚어요", mem, ask_profile=True)
    assert pl.request.profile.mobility == "walker" and pl.request.region == "제주시"


def test_plain_no_still_keeps_course(orch):
    """조건 낱말 없이 '없어'만 하면 예전처럼 코스를 그대로 둔다(조건 빼기로 읽지 않음)."""
    mem: dict = {}
    orch.ask("휠체어로 서귀포 2일 코스 짜줘", mem)
    pl = orch.plan("없어", mem)
    assert "뺀 조건" not in pl.understood
