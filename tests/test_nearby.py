"""근처 식당·카페·숙소 질문(nearby): 질문 분류 → 장소 찾기(access)가 주변정보로 답한다."""
from jeju_agent.contracts import Intent, PlaceItem
from jeju_agent.orchestrator import parse_nearby_kinds


def test_kinds_from_question():
    assert parse_nearby_kinds("근처 식당이랑 숙소") == ["restaurant", "lodging"]
    assert parse_nearby_kinds("카페 있어?") == ["cafe"]
    assert parse_nearby_kinds("근처에 뭐 있어") == []


def test_region_question_answers_with_n1_and_passes_verification(orch):
    r = orch.ask("서귀포 숙소 알려줘", {})
    assert r.plan.request.intent == Intent.NEARBY
    res = r.results[-1]
    assert res.agent == "access" and res.status == "ok"
    assert {e.id for e in res.evidence} == {"N1", "N2"}
    shops = [i for i in res.payload["items"] if i["kind"] != "attraction"]
    assert shops and all(i["kind"] == "lodging" and i["verdict"] != "부적합" for i in shops)
    for i in res.payload["items"]:
        PlaceItem(**i)
    assert "[근거표 N1]" in r.answer and r.verification.passed


def test_cafe_only_is_not_crowded_out_by_restaurants(orch):
    shops = [i for i in orch.ask("카페 찾아줘", {}).results[-1].payload["items"] if i["kind"] != "attraction"]
    assert shops and all(i["kind"] == "cafe" for i in shops)


def test_needed_toilet_drops_confirmed_no(orch):
    r = orch.ask("장애인 화장실 있는 식당 찾아줘", {}).results[-1]
    shops = [i for i in r.payload["items"] if i["kind"] != "attraction"]
    assert all(i["facilities"][0]["state"] == "있음" for i in shops)


def test_place_without_nearby_data_falls_back_to_close_course(orch):
    r = orch.ask("이호테우해변 근처 숙소 알려줘", {})
    assert r.plan.request.place_ids                      # '○○ 근처'를 기준 장소로 읽음
    res = r.results[-1]
    assert res.status == "ok" and "조사 자료에 없어" in res.findings[0].text
    assert all(i["anchor_id"] != r.plan.request.place_ids[0] for i in res.payload["items"] if i["kind"] != "attraction")


def test_no_shops_in_region_is_honest_and_passes_verification(orch):
    r = orch.ask("성산 근처 밥 먹을 데", {})
    assert r.results[-1].status == "insufficient" and "찾지 못했습니다" in r.answer
    assert r.verification.passed
