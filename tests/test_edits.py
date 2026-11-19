"""수정 대화: 직전 코스를 말로 고치기('둘째 날 빡빡해', '○○ 빼줘', '엄마도 같이 가')와 장소 하나 물어보기('○○ 화장실 있어?')."""
import pandas as pd

from jeju_agent.edits import match_places, parse_edit
from jeju_agent.llm import LLMClient
from jeju_agent.orchestrator import Orchestrator

PLACES = pd.DataFrame({"place_id": ["P1", "P2", "P3", "P4"],
                       "name": ["천지연폭포", "보목포로(볼레낭개길)", "사려니숲길-무장애나눔길", "사려니숲길-1(절물휴양림방향)"]})


def course(orch, q="휠체어로 서귀포 3일 코스 짜줘"):
    mem: dict = {}
    orch.ask(q, mem)
    return mem


def ok_all(resp):
    return all(c.ok for c in resp.verification.checks), [c.name for c in resp.verification.checks if not c.ok]


# ── 해석 ──────────────────────────────────────────────────
def test_match_places_partial_and_parentheses():
    assert [h["place_id"] for h in match_places("천지연 화장실 있어?", PLACES)] == ["P1"]
    assert [h["place_id"] for h in match_places("보목포로 빼줘", PLACES)] == ["P2"]      # 괄호 안 설명은 빼고 맞춘다
    assert len(match_places("사려니숲길 빼줘", PLACES)) == 2                              # 똑같이 맞으면 둘 다 → 되묻기
    assert match_places("화장실 있어?", PLACES) == []


def test_parse_edit_kinds(orch):
    mem = course(orch)
    names = PLACES  # 해석만 보는 테스트라 작은 표로 충분
    assert parse_edit("둘째 날 빡빡해", mem, names) == {"kind": "lighter", "day": 2}
    assert parse_edit("너무 빡빡해", mem, names) == {"kind": "lighter_all"}
    assert parse_edit("엄마도 같이 가", mem, names) == {"kind": "join", "who": "엄마", "n": 1}
    assert parse_edit("부모님이랑 같이 가", mem, names)["n"] == 2
    assert parse_edit("천지연 화장실 있어?", mem, names)["kind"] == "place_q"
    assert parse_edit("사려니숲길 빼줘", mem, names)["kind"] == "ask"
    assert parse_edit("제주도 가고 싶어", mem, names) is None          # '도 가'지만 사람이 아님


def test_edit_needs_a_course_except_place_question(orch):
    assert parse_edit("둘째 날 빡빡해", {}, PLACES) is None            # 고칠 코스가 없으면 보통 질문으로
    assert parse_edit("천지연 화장실 있어?", {}, PLACES)["kind"] == "place_q"


def test_days_change_is_not_lighter(orch):
    mem = course(orch)
    plan = orch.plan("2일로 줄여줘", mem)
    assert not plan.request.params.get("edit") and plan.request.days == 2


# ── 코스 고치기 ───────────────────────────────────────────
def test_lighter_day_and_diff_table(orch):
    mem = course(orch)
    before = [p for p in mem["last_itinerary"] if p["day"] == 2]
    r = orch.ask("둘째 날 빡빡해", mem)
    c = r.results[-1]
    after = [p for p in c.payload["itinerary"] if p["day"] == 2]
    assert len(after) == len(before) - 1 and c.payload["day_caps"] == {2: len(before) - 1}
    c6 = next(t for t in c.evidence if t.id == "C6")
    assert c6.columns[:3] == ["일차", "고치기 전", "고친 뒤"]
    assert c.findings[0].evidence_ids == ["C6"] and "2일차" in c.findings[0].text
    assert ok_all(r)[0], ok_all(r)[1]
    assert r.plan.parser == "rule"


def test_exclude_persists_across_edits_and_follow_ups(orch):
    mem = course(orch)
    gone = mem["last_itinerary"][-1]
    orch.ask(f"{gone['name']} 빼줘", mem)
    assert gone["place_id"] not in {p["place_id"] for p in mem["last_itinerary"]}
    orch.ask("둘째 날 빡빡해", mem)                                     # 다음 수정에도 계속 빠짐
    assert gone["place_id"] not in {p["place_id"] for p in mem["last_itinerary"]}
    r = orch.ask("다시 버스로", mem)                                    # 조건만 바꾼 이어 묻기에도
    assert r.plan.request.profile.transport == "bus"
    assert gone["place_id"] not in {p["place_id"] for p in mem["last_itinerary"]}
    assert ok_all(r)[0], ok_all(r)[1]


def test_join_keeps_user_mobility_and_adds_companion(orch):
    mem = course(orch)
    r = orch.ask("할머니도 같이 가", mem)
    prof = r.plan.request.profile
    assert prof.mobility == "wheelchair_manual" and prof.companions == 2   # 할머니 말이 이용자 휠체어 조건을 덮지 않음
    assert "할머니" in r.results[-1].findings[0].text
    assert ok_all(r)[0], ok_all(r)[1]


def test_edit_plan_text_and_single_llm_call(orch, api_settings, repo, tmp_path, monkeypatch):
    """API 모드 대화 중에는 LLM이 먼저 한 번(dialog) 해석한다. 실패하면 규칙 해석으로 같은 수정이 된다(질문 해석 호출은 더 하지 않음)."""
    mem = course(orch)
    api = Orchestrator(api_settings, repo=repo, llm=LLMClient(api_settings, runtime_dir=tmp_path / "api"))
    calls = []
    monkeypatch.setattr(api.llm, "chat_json", lambda *a, **k: calls.append(k.get("purpose")) or (None, None))
    plan = api.plan("둘째 날 빡빡해", mem)
    assert calls == ["dialog"] and plan.understood.startswith("직전 코스 고치기: 2일차 장소 1곳 줄이기")


def test_verify_catches_excluded_place_back(orch):
    from jeju_agent.verify import _course_violations
    mem = course(orch)
    r = orch.ask(f"{mem['last_itinerary'][0]['name']} 빼줘", mem)
    c = r.results[-1].model_copy(deep=True)
    c.payload["excluded_ids"] = [c.payload["itinerary"][0]["place_id"]]   # 뺀 장소가 다시 들어간 결과를 흉내
    assert any("뺀 장소" in b for b in _course_violations(c, r.plan))


# ── 장소 하나 물어보기 ────────────────────────────────────
def test_place_question_answers_with_evidence_and_keeps_course(orch):
    mem = course(orch)
    it = list(mem["last_itinerary"])
    name = it[0]["name"]
    r = orch.ask(f"{name} 화장실 있어?", mem)
    a = r.results[-1]
    assert r.plan.request.place_ids == [it[0]["place_id"]] and a.payload["place_q"] == "acc_toilet"
    a5 = next(t for t in a.evidence if t.id == "A5")
    assert a5.rows[0][2] in ("있음", "없음", "정보 없음")
    assert a.findings[0].evidence_ids == ["A5"] and name in a.findings[0].text
    assert ok_all(r)[0], ok_all(r)[1]
    assert mem["last_itinerary"] == it and mem["last_intent"] == "course"   # 직전 코스를 덮지 않음
    assert orch.plan("둘째 날 빡빡해", mem).request.params["edit"]["kind"] == "lighter"


def test_place_question_unknown_never_says_absent(orch):
    """정보 없음이면 '없다'고 말하지 않는다."""
    places = orch.repo.places()
    row = places[places["elevator"].isna()].iloc[0]
    r = orch.ask(f"{row['name']} 엘리베이터 있어?", {})
    text = r.results[-1].findings[0].text
    assert "언급이 없습니다" in text and "없다는 뜻은 아니" in text
    assert ok_all(r)[0], ok_all(r)[1]


def test_ambiguous_place_asks_back(orch):
    mem = course(orch)
    names = orch.repo.places()["name"]
    if not (names.str.startswith("사려니숲길").sum() >= 2):
        return
    r = orch.ask("사려니숲길 빼줘", mem)
    assert "어느 곳을 뺄까요" in r.answer and "사려니숲길" in r.answer


# ── Cursor 리뷰 반영(11/16) ──────────────────────────────
def test_join_wins_over_lighter_words(orch):
    """'같이 가는데 … 힘들어'는 일행 추가. '볼 게 많아'는 수정이 아님."""
    mem = course(orch)
    assert parse_edit("할머니도 같이 가는데 오래 걷기 힘들어", mem, PLACES)["kind"] == "join"
    assert parse_edit("할머니가 힘들어", mem, PLACES)["kind"] == "ask"       # 어느 날인지 모르면 되묻기
    assert parse_edit("볼 게 많아", mem, PLACES) is None


def test_join_reads_walking_condition_when_user_has_none(orch):
    mem = course(orch, "제주시 2일 코스 짜줘")
    r = orch.ask("엄마도 같이 가는데 오래 걷기 힘들어", mem)
    assert r.plan.request.profile.mobility == "walker" and r.plan.request.profile.companions == 2


def test_missing_day_asks_back_and_keeps_course(orch):
    mem = course(orch)                                                  # 3일 코스
    before = list(mem["last_itinerary"])
    r = orch.ask("다섯째 날 빡빡해", mem)
    assert "5일차가 없어요" in r.answer and r.results == []
    assert mem["last_itinerary"] == before and mem["last_intent"] == "course"


def test_partial_name_needs_confirm_full_name_does_not(orch):
    mem = course(orch)
    name = next(p["name"] for p in mem["last_itinerary"] if "(" in p["name"] or len(p["name"]) >= 5)
    short = name.split("(")[0][:max(3, (len(name.split("(")[0]) + 1) // 2 + 1)]
    part = orch.plan(f"{short} 빼줘", mem).request.params["edit"]
    full = orch.plan(f"{name} 빼줘", mem).request.params["edit"]
    assert part["kind"] == full["kind"] == "exclude"
    assert part["confirm"] is (short != name.split("(")[0]) and full["confirm"] is False


def test_include_undoes_exclude(orch):
    mem = course(orch)
    gone = mem["last_itinerary"][0]
    orch.ask(f"{gone['name']} 빼줘", mem)
    assert gone["place_id"] in mem["edits"]["exclude"]
    r = orch.ask(f"{gone['name']} 다시 넣어줘", mem)
    assert r.plan.request.params["edit"]["kind"] == "include"
    assert gone["place_id"] not in mem["edits"]["exclude"]
    assert ok_all(r)[0], ok_all(r)[1]


def test_follow_up_keeps_lighter_days_and_pace(orch):
    """둘째 날을 줄인 뒤 '다시 버스로'만 바꿔도 그날은 다시 늘지 않는다."""
    mem = course(orch, "서귀포 3일 코스 짜줘")
    orch.ask("둘째 날 빡빡해", mem)
    n2 = sum(1 for p in mem["last_itinerary"] if p["day"] == 2)
    orch.ask("너무 빡빡해", mem)
    r = orch.ask("다시 버스로", mem)
    assert r.plan.request.params["edits"]["day_caps"] == {2: n2} and r.plan.request.params["edits"]["pace"] == "여유"
    assert sum(1 for p in mem["last_itinerary"] if p["day"] == 2) <= n2
    assert r.results[-1].payload["pace"] == "여유"
    r = orch.ask("1일로 줄여줘", mem)                                   # 없어진 날의 '줄인 날'은 버린다
    assert r.plan.request.params["edits"]["day_caps"] == {}


# ── 확실할 때만 고치고, 아니면 되묻기(11/16) ──────────────────
def test_unclear_requests_ask_and_keep_course(orch):
    mem = course(orch)                                                  # 3일 코스
    before = list(mem["last_itinerary"])
    name = before[0]["name"]
    for q, word in [("할머니가 힘들어", "어느 날을 줄일까요"), ("6일차 빡빡해", "6일차가 없어요"), ("빼줘", "어느 장소를 뺄까요"),
                    (f"{name} 빼고 버스로", "하나씩"), (f"둘째 날 빡빡하고 {name} 빼줘", "하나씩"), (f"{name} 다시 넣어줘", "뺀 장소가 없어요")]:
        r = orch.ask(q, mem)
        assert word in r.answer and r.results == [], q
        assert mem["last_itinerary"] == before and mem["last_intent"] == "course", q   # 코스는 그대로


def test_ask_offers_choices_that_work(orch):
    """되묻기의 답 버튼은 그대로 눌러 실행되는 말이어야 한다."""
    mem = course(orch)
    e = orch.plan("할머니가 힘들어", mem).request.params["edit"]
    assert e["choices"] == ["1일차 줄여줘", "2일차 줄여줘", "3일차 줄여줘", "모든 날 줄여줘"]
    assert orch.plan(e["choices"][1], mem).request.params["edit"] == {"kind": "lighter", "day": 2}
    assert orch.plan(e["choices"][-1], mem).request.params["edit"] == {"kind": "lighter_all"}
    e = orch.plan("빼줘", mem).request.params["edit"]
    assert len(e["choices"]) == len(mem["last_itinerary"])
    assert orch.plan(e["choices"][0], mem).request.params["edit"]["kind"] == "exclude"


def test_words_inside_other_words_are_not_edits(orch):
    mem = course(orch)
    for q in ("편하게 살려고 왔어", "볼 게 많아", "버스 말고 택시로", "2일로 줄여줘", "사진도 포함해서 알려줘",
              "실내도 포함해야 해?", "비 오는 날도 포함해도 돼?", "주말 제외하고 알려줘"):
        assert not (orch.plan(q, mem).request.params.get("edit")), q


def test_partial_name_confirms_for_include_and_place_question(orch):
    mem = course(orch)
    p = next(x for x in mem["last_itinerary"] if len(x["name"].split("(")[0]) >= 5)
    base = p["name"].split("(")[0]
    short = base[: len(base) - 1]                                       # 이름 일부만
    assert orch.plan(f"{short} 화장실 있어?", mem).request.params["edit"]["confirm"] is True
    orch.ask(f"{p['name']} 빼줘", mem)
    assert orch.plan(f"{short} 다시 넣어줘", mem).request.params["edit"]["confirm"] is True
    assert orch.plan(f"{base} 다시 넣어줘", mem).request.params["edit"]["confirm"] is False


def test_include_word_needs_its_ending(orch):
    """'포함·제외'는 부탁하는 끝맺음(포함해 줘·포함해·포함시켜)일 때만 동작. 포함해서·포함해야·포함해도는 아니다."""
    mem = course(orch)
    gone = mem["last_itinerary"][0]
    orch.ask(f"{gone['name']} 빼줘", mem)
    assert (orch.plan(f"{gone['name']} 사진도 포함해서 알려줘", mem).request.params.get("edit") or {}).get("kind") != "include"
    for q in (f"{gone['name']} 포함해야 하나?", f"{gone['name']} 포함해도 돼?"):
        assert (orch.plan(q, mem).request.params.get("edit") or {}).get("kind") != "include", q
    for q in (f"{gone['name']} 포함해줘", f"{gone['name']} 포함해", f"{gone['name']} 포함시켜 주세요"):
        assert orch.plan(q, mem).request.params["edit"]["kind"] == "include", q
