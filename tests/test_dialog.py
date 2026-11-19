"""대화 이어가기 — 되물은 질문 기억, 한 날만 고치기, 조건 이어받기, LLM 대화 해석(가짜 모델).

PDF로 받은 실제 대화('2일차만 바꿔줘볼래?' → 같은 코스 반복, '2일차만 세곳을 가게 해줘' → 조건 되묻기,
'없어 이동에 불편한건 없어' → 답할 수 없는 질문)가 다시 생기지 않는지 본다.
"""
import dataclasses
import json

from jeju_agent.contracts import Intent, TravelerProfile
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import LLMPlan, Orchestrator


def course(orch, q="휠체어로 서귀포 2일 코스 짜줘"):
    mem: dict = {}
    orch.ask(q, mem)
    return mem


def go(orch, q, mem):
    return orch.execute(orch.plan(q, mem, ask_profile=True), mem)


def by_day(resp) -> dict[int, list[str]]:
    it = resp.results[-1].payload["itinerary"]
    return {d: [p["name"] for p in it if p["day"] == d] for d in sorted({p["day"] for p in it})}


def edit_of(resp) -> dict:
    return resp.plan.request.params.get("edit") or {}


def all_ok(resp) -> bool:
    return all(c.ok for c in resp.verification.checks)


# ── 날을 가리키는 말 ───────────────────────────────────────
def test_vague_day_change_asks_instead_of_repeating_same_course(orch):
    mem = course(orch)
    before = [p["name"] for p in mem["last_itinerary"]]
    r = go(orch, "2일차만 바꿔줘볼래?", mem)
    e = edit_of(r)
    assert e["kind"] == "ask" and "2일차를 어떻게" in e["message"]
    assert "2일차 다른 곳으로 바꿔줘" in e["choices"]
    assert [p["name"] for p in mem["last_itinerary"]] == before     # 코스는 그대로
    assert mem["pending"]["day"] == 2


def test_answer_to_day_question_keeps_the_day(orch):
    mem = course(orch)
    go(orch, "2일차만 바꿔줘볼래?", mem)
    r = go(orch, "세 곳으로", mem)                                   # '2일차 세 곳으로'로 읽는다
    assert edit_of(r)["kind"] == "day_count" and r.plan.request.params["said"] == "세 곳으로"
    assert len(by_day(r)[2]) == 3 and all_ok(r)                       # 속도(여유, 하루 2곳)보다 많아도 이용자가 정한 수
    assert "바뀐 날: 2일차" in r.answer


def test_one_day_edit_keeps_other_days(orch):
    mem = course(orch)
    day1 = [p["name"] for p in mem["last_itinerary"] if p["day"] == 1]
    r = go(orch, "2일차 다른 곳으로 바꿔줘", mem)
    d = by_day(r)
    assert d[1] == day1 and edit_of(r)["kind"] == "day_swap" and all_ok(r)
    assert not set(d[2]) & set(edit_of(r)["names"])                   # 그날 장소는 모두 바뀜
    assert "다른 날은 그대로" in r.answer


def test_lighter_one_day_does_not_spill_to_next_day(orch):
    mem = course(orch, "휠체어로 서귀포 2일 코스 짜줘. 2일차 3곳으로")
    mem = course(orch)
    go(orch, "1일차 3곳으로 해줘", mem)
    day2 = [p["name"] for p in mem["last_itinerary"] if p["day"] == 2]
    r = go(orch, "1일차가 너무 빡빡해. 1일차 일정만 바꿔줘", mem)
    assert edit_of(r)["kind"] == "lighter" and by_day(r)[2] == day2
    assert "바뀐 날: 1일차" in r.answer and "바뀐 날은" not in r.answer   # '바뀐 날은 2일'(2일차로 읽힘) 문구 없음


def test_same_count_does_not_replan(orch):
    mem = course(orch)
    before = list(mem["last_itinerary"])
    r = go(orch, "2일차 두 곳으로 해줘", mem)
    assert edit_of(r)["kind"] == "ask" and mem["last_itinerary"] == before


def test_merge_days_asks(orch):
    mem = course(orch)
    e = edit_of(go(orch, "1일차랑 2일차 합쳐줘", mem))
    assert e["kind"] == "ask" and e["choices"] == ["1일 코스로 다시 짜줘"]


# ── 조건 되묻기·이어받기 ─────────────────────────────────────
def test_typed_answer_to_profile_question_runs_original_question(orch):
    mem: dict = {}
    assert edit_of(go(orch, "애월 갈 만한 곳", mem)).get("for") == "profile"
    r = go(orch, "없어 이동에 불편한건 없어", mem)
    assert r.plan.request.intent == Intent.ACCESS and r.plan.request.region == "애월읍"
    assert r.plan.request.params["said"] == "없어 이동에 불편한건 없어"


def test_typed_condition_answer(orch):
    mem: dict = {}
    go(orch, "애월 갈 만한 곳", mem)
    r = go(orch, "휠체어 타요", mem)
    assert r.plan.request.intent == Intent.ACCESS and r.plan.request.profile.mobility == "wheelchair_manual"


def test_new_question_after_profile_question_is_not_forced_as_answer(orch):
    mem: dict = {}
    go(orch, "애월 갈 만한 곳", mem)
    r = go(orch, "제주시 읍면동별 장애인 화장실 현황", mem)
    assert r.plan.request.intent == Intent.STATUS and "said" not in r.plan.request.params


def test_conditions_carry_over_instead_of_asking_again(orch):
    mem = course(orch)
    r = go(orch, "애월 갈 만한 곳", mem)
    assert r.plan.request.intent == Intent.ACCESS
    assert r.plan.request.profile.mobility == "wheelchair_manual" and "앞 대화의 조건" in r.plan.understood


def test_unrelated_words_are_still_refused_offline(orch):
    mem = course(orch)
    r = go(orch, "편하게 살려고 왔어", mem)
    assert r.plan.request.intent == Intent.UNSUPPORTED and not edit_of(r)


# ── LLM 대화 해석 (가짜 모델) ──────────────────────────────
def llm_orch(api_settings, repo, tmp_path, dialog_reply: dict, seen: list):
    s = dataclasses.replace(api_settings, llm_cache=False, llm_max_calls_per_question=6, llm_tool_calling=False)

    def transport(messages, model):
        system = messages[0]["content"]
        if "대화 해석기" in system:
            got = json.loads(messages[1]["content"])
            seen.append(got)
            reply = dialog_reply(got) if callable(dialog_reply) else dialog_reply
            return json.dumps(reply, ensure_ascii=False), 50, 10
        if "질문 해석기" in system:   # 맥락 없는 첫 질문: 규칙 결과와 같게
            q = messages[1]["content"]
            if "코스" in q:
                return json.dumps({"intent": "course", "days": 2, "region": "서귀포시"}), 50, 10
            return json.dumps({"intent": "access", "region": "애월읍"}), 50, 10
        return "설명", 50, 5   # 설명은 숫자 대조에서 떨어져 문장틀로 돌아간다

    return Orchestrator(s, repo=repo, llm=LLMClient(s, runtime_dir=tmp_path, transport=transport))


def test_llm_maps_free_words_to_supported_edit(api_settings, repo, tmp_path):
    seen: list = []
    o = llm_orch(api_settings, repo, tmp_path, {"command": "2일차 다른 곳으로 바꿔줘"}, seen)
    mem = course(o)
    r = go(o, "둘째 날 장소들은 좀 덜 끌리네", mem)            # 규칙 낱말에 없는 말
    assert r.plan.parser == "llm" and edit_of(r)["kind"] == "day_swap"
    assert r.plan.request.params["said"] == "둘째 날 장소들은 좀 덜 끌리네"
    ctx = seen[-1]["context"]
    assert [d["day"] for d in ctx["course"]] == [1, 2] and ctx["conditions"]   # 대화 맥락(코스·조건)을 받았다
    assert "verdicts" not in json.dumps(seen[-1], ensure_ascii=False)          # 관광지 원본 값은 보내지 않는다
    assert any(t.action == "dialog" for t in r.trace)


def test_llm_command_is_rechecked_by_rules(api_settings, repo, tmp_path):
    o = llm_orch(api_settings, repo, tmp_path, {"command": "아무렇게나 해"}, [])
    mem = course(o)
    before = list(mem["last_itinerary"])
    r = go(o, "둘째 날 장소들은 좀 덜 끌리네", mem)
    assert edit_of(r)["kind"] == "ask" and mem["last_itinerary"] == before   # 규칙이 못 읽는 문장은 실행하지 않고 되묻는다


def test_llm_ask_keeps_only_readable_choices(api_settings, repo, tmp_path):
    reply = {"ask": "어느 날을 바꿀까요?", "choices": ["1일차 다른 곳으로 바꿔줘", "하늘을 날게 해줘"]}
    o = llm_orch(api_settings, repo, tmp_path, reply, [])
    mem = course(o)
    e = edit_of(go(o, "좀 별로야 다시", mem))
    assert e["kind"] == "ask" and e["choices"] == ["1일차 다른 곳으로 바꿔줘"]


def test_day_reference_is_not_trip_length(orch):
    req = orch._rule_parse("1일차랑 2일차는 실내로", {})
    got = LLMPlan(intent="course", days=3)
    assert orch._merge(req, got, {}).days is None


def test_day_count_respected_by_verifier(orch):
    mem = course(orch)
    r = go(orch, "1일차 세 곳으로 해줘", mem)
    assert len(by_day(r)[1]) == 3 and all_ok(r)
    assert r.results[-1].payload["day_caps"] == {1: 3}
    assert TravelerProfile(**r.plan.request.profile.model_dump()).mobility == "wheelchair_manual"


def test_plain_no_to_day_question_keeps_course(orch):
    mem = course(orch)
    before = list(mem["last_itinerary"])
    go(orch, "2일차만 바꿔줘볼래?", mem)
    r = go(orch, "없어 이동에 불편한건 없어", mem)
    assert "그대로 둘게요" in r.answer and mem["last_itinerary"] == before
    assert "pending" not in mem


# ── 코스에 이어 묻기 (두 번째 PDF) ─────────────────────────
def test_course_question_without_days_continues_current_course(orch):
    mem = course(orch)
    go(orch, "1일차만 다른 코스로 추천해줄래?", mem)
    before = by_day_mem = {d: [p["name"] for p in mem["last_itinerary"] if p["day"] == d] for d in (1, 2)}
    r = go(orch, "그럼 여행코스에 숙소 하나랑 각각 점심 저녁 식당까지 포함해서 짜줄래?", mem)
    rq = r.plan.request
    assert rq.days == 2 and rq.params.get("course_from_chat") and "지금 코스에 이어서" in r.plan.understood
    assert by_day(r) == before == by_day_mem                       # 장소는 그대로(뺀 곳도 다시 안 들어옴)


def test_new_days_makes_a_new_course(orch):
    mem = course(orch)
    r = go(orch, "그럼 3일 코스로 짜줘", mem)
    assert r.plan.request.days == 3


def test_nearby_without_anchor_uses_course_places(orch):
    mem = course(orch)
    ids = list(mem["last_place_ids"])
    r = go(orch, "혹시 주변 식당이랑 숙소까지 고려해줄 수 있어?", mem)
    assert r.plan.request.intent == Intent.NEARBY and r.plan.request.place_ids == ids
    assert "지금 코스 장소 근처" in r.plan.understood


# ── API 모드: 대화 중에는 LLM이 먼저, 규칙이 검증 ─────────────────
def test_llm_first_continues_course_for_meals(api_settings, repo, tmp_path):
    reply = {"type": "question", "question": "서귀포 2일 코스에 숙소와 점심 식당도 넣어 짜줘",
             "plan": {"intent": "course", "days": 2, "region": "서귀포시", "follow_up": True}}
    o = llm_orch(api_settings, repo, tmp_path, reply, [])
    mem = course(o)
    before = {d: [p["name"] for p in mem["last_itinerary"] if p["day"] == d] for d in (1, 2)}
    r = go(o, "그럼 여행코스에 숙소 하나랑 점심 식당까지 포함해서 짜줄래?", mem)
    assert r.plan.parser == "llm" and r.plan.request.days == 2 and by_day(r) == before   # 새 1일 코스가 아니라 지금 코스 그대로
    assert r.plan.request.params["said"].startswith("그럼 여행코스에")


def test_llm_first_reads_typed_answer_to_profile_question(api_settings, repo, tmp_path):
    def reply(got):
        assert got["context"]["you_asked"] == "애월 갈 만한 곳"
        return {"type": "question", "question": "애월 갈 만한 곳 (불편한 점 없음)",
                "plan": {"intent": "access", "region": "애월읍", "mobility": "none"}}
    o = llm_orch(api_settings, repo, tmp_path, reply, [])
    mem: dict = {}
    assert edit_of(go(o, "애월 갈 만한 곳", mem)).get("for") == "profile"
    r = go(o, "없어 딱히", mem)
    assert r.plan.request.intent == Intent.ACCESS and r.plan.request.region == "애월읍" and not edit_of(r)


def test_llm_cannot_drop_users_conditions(api_settings, repo, tmp_path):
    reply = {"type": "question", "question": "애월 갈 만한 곳", "plan": {"intent": "access", "region": "애월읍", "mobility": "none"}}
    o = llm_orch(api_settings, repo, tmp_path, reply, [])
    mem = course(o)                                   # 휠체어 조건으로 시작
    r = go(o, "애월도 가 보고 싶어", mem)
    assert r.plan.request.profile.mobility == "wheelchair_manual"   # LLM이 'none'이라 해도 앞 대화의 안전 조건은 남는다


def test_llm_question_that_is_really_an_edit_becomes_edit(api_settings, repo, tmp_path):
    reply = {"type": "question", "question": "2일차 3곳으로 해줘", "plan": {"intent": "course"}}
    o = llm_orch(api_settings, repo, tmp_path, reply, [])
    mem = course(o)
    r = go(o, "둘째 날 하나 더 넣자", mem)
    assert edit_of(r)["kind"] == "day_count" and len(by_day(r)[2]) == 3


def test_llm_edit_with_unknown_place_does_not_change_course(api_settings, repo, tmp_path):
    o = llm_orch(api_settings, repo, tmp_path, {"type": "edit", "command": "없는관광지이름 빼줘"}, [])
    mem = course(o)
    before = list(mem["last_itinerary"])
    r = go(o, "그 이상한 데 빼", mem)
    assert edit_of(r)["kind"] == "ask" and mem["last_itinerary"] == before


def test_llm_first_uses_one_call_per_turn(api_settings, repo, tmp_path):
    o = llm_orch(api_settings, repo, tmp_path, {"type": "edit", "command": "1일차 빡빡해"}, [])
    mem = course(o)
    log = tmp_path / "llm_usage.jsonl"
    n0 = len(log.read_text(encoding="utf-8").splitlines())
    r = go(o, "첫날 좀 줄이자", mem)
    purposes = [json.loads(x)["purpose"] for x in log.read_text(encoding="utf-8").splitlines()[n0:]]
    assert edit_of(r)["kind"] == "lighter"
    assert purposes.count("dialog") == 1 and purposes.count("plan") == 0   # 해석 호출은 한 번(질문 해석을 따로 하지 않음)


def test_dialog_context_tells_llm_meals_were_missing(api_settings, repo, tmp_path):
    seen: list = []
    o = llm_orch(api_settings, repo, tmp_path, {"type": "ask", "ask": "알겠어요. 코스는 그대로 둘게요."}, seen)
    mem = course(o)
    r = go(o, "주변 식당도 고려해줄 수 있어?", mem)
    ctx = seen[-1]["context"]
    assert "meals_in_course" in ctx and "lodging_in_course" in ctx
    assert r.llm_usage.calls >= 1          # 되묻기로 끝나도 해석 호출 비용을 센다


def test_meal_words_in_course_question():
    from jeju_agent.orchestrator import parse_meal_wants
    assert parse_meal_wants("숙소 하나랑 각각 점심 저녁 식당까지 포함해서") == ["lunch", "dinner", "lodging"]
    assert parse_meal_wants("식당도 넣어줘") == ["lunch"]
    assert parse_meal_wants("저녁 식당만") == ["dinner"]
    assert parse_meal_wants("바다 보는 코스") == []


# ── 자료에 없는 장소 ───────────────────────────────────────
def test_unknown_place_question_keeps_course(orch):
    """자료에 없는 곳('성산일출봉')을 물으면 지역 장소 찾기로 바꾸지 말고 '자료에 없음'이라고 답함, 코스는 그대로"""
    mem = course(orch)
    before = list(mem["last_itinerary"])
    r = go(orch, "성산일출봉 화장실 있어?", mem)
    e = edit_of(r)
    assert r.plan.request.intent == Intent.UNSUPPORTED and e["kind"] == "ask"
    assert "성산일출봉" in e["message"] and "자료에 없" in e["message"]
    assert mem["last_itinerary"] == before and mem["last_intent"] == "course"


def test_unknown_place_first_question_is_honest(orch):
    r = go(orch, "만장굴 주차 돼?", {})
    e = edit_of(r)
    assert r.plan.request.intent == Intent.UNSUPPORTED and "만장굴" in e["message"] and "자료에 없" in e["message"]


def test_unknown_place_suggests_places_in_same_area(orch):
    """이름에 지역(성산)이 들어 있으면 그 읍면동의 자료 장소를 고를 답으로 줌 (규칙이 그대로 읽는 문장)"""
    r = go(orch, "성산일출봉 화장실 있어?", {})
    choices = edit_of(r)["choices"]
    assert choices and all(c.endswith("화장실 있어?") for c in choices)
    r2 = go(orch, choices[0], {})
    assert edit_of(r2)["kind"] == "place_q"


def test_known_place_and_region_questions_unchanged(orch):
    assert edit_of(go(orch, "천지연폭포 화장실 있어?", {}))["kind"] == "place_q"
    assert edit_of(go(orch, "송악산 화장실 있어?", {}))["kind"] == "place_q"          # '송악산전망대'로 맞음
    r = go(orch, "성산 쪽 화장실 있는 곳 알려줘", {})
    assert r.plan.request.intent == Intent.ACCESS and r.plan.request.region == "성산읍"
    r = go(orch, "성산 화장실 있어?", {})                                            # 지역 이름만이면 지역 찾기
    assert r.plan.request.intent == Intent.ACCESS
    r = go(orch, "성산일출봉 근처 화장실 있는 식당 있어?", {})                      # 근처 가게 질문은 그대로
    assert r.plan.request.intent == Intent.NEARBY


def test_llm_rewrite_of_unknown_place_still_keeps_course(api_settings, repo, tmp_path):
    """LLM이 '성산읍에서 화장실 있는 곳'으로 바꿔 써도 이용자가 한 말에 자료에 없는 장소가 있으면 같은 답"""
    reply = {"type": "question", "question": "성산읍에서 장애인 화장실 있는 곳 찾아줘", "plan": {"intent": "access", "region": "성산읍"}}
    o = llm_orch(api_settings, repo, tmp_path, reply, [])
    mem = course(o)
    before = list(mem["last_itinerary"])
    r = go(o, "성산일출봉 화장실 있어?", mem)
    assert r.plan.request.intent == Intent.UNSUPPORTED and "자료에 없" in edit_of(r)["message"]
    assert mem["last_itinerary"] == before
