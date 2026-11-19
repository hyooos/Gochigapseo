import pytest
from pydantic import ValidationError

from jeju_agent.contracts import NEARBY_KIND, AgentResult, AnalysisRequest, Intent, PlaceItem, TravelerProfile
from jeju_agent.orchestrator import AGENT_FOR, INTENT_KO, STEPS


def test_request_defaults():
    r = AnalysisRequest(question="q", intent=Intent.ACCESS)
    assert r.region is None and r.place_ids == [] and r.profile.is_default() and r.days is None


def test_result_status_is_validated():
    with pytest.raises(ValidationError):
        AgentResult(agent="access", status="maybe", summary="x")  # type: ignore[arg-type]


def test_profile_rejects_unknown_values_and_bad_ranges():
    with pytest.raises(ValidationError):
        TravelerProfile(mobility="jetpack")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TravelerProfile(needs=["helicopter"])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        AnalysisRequest(question="q", intent=Intent.COURSE, days=9)


def test_profile_describe_skips_defaults_and_dedupes_needs():
    p = TravelerProfile(mobility="wheelchair_manual", needs=["acc_toilet", "acc_toilet"], transport="bus", companions=3)
    assert p.needs == ["acc_toilet"]
    assert p.describe() == ["수동 휠체어", "장애인 화장실 필요", "버스 이동", "3명"]
    assert TravelerProfile().describe() == []


def test_every_intent_has_label_and_steps():
    for i in Intent:
        assert i in INTENT_KO and i in STEPS


def test_nearby_intent_is_handled_by_access_agent():
    assert AGENT_FOR[Intent.NEARBY] == "access"


def test_place_item_shape_for_nearby_place():
    it = PlaceItem(item_id="C01-N1", name="금돈가", kind=NEARBY_KIND["음식점"], category="음식점", verdict="적합",
                   anchor_id="C01", distance_km=0.5, source="무장애여행코스 주변정보", data_date="2021-12-22")
    assert it.kind == "restaurant" and it.lat is None and it.phone is None and it.facilities == []
    with pytest.raises(ValidationError):
        PlaceItem(item_id="x", name="x", kind="souvenir", verdict="적합", source="s")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PlaceItem(item_id="x", name="x", kind="cafe", verdict="없음", source="s")  # type: ignore[arg-type]
