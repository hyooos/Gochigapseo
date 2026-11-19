"""프로필 되묻기: 채팅에서 장소·코스 질문에 이동·감각 조건이 하나도 없으면 대화에서 한 번만 버튼으로 묻는다."""
import pytest


def ask(orch, q, mem):
    return orch.plan(q, mem, ask_profile=True)


def test_asks_once_when_no_condition(orch):
    mem: dict = {}
    p = ask(orch, "서귀포 2일 코스 짜줘", mem)
    e = p.request.params["edit"]
    assert e["kind"] == "ask" and "불편한 점" in p.refusal_reason
    assert [c["label"] for c in e["choices"]][-1].startswith("불편한 점 없음")
    assert ask(orch, "제주시 갈 만한 곳 추천해줘", mem).request.intent.value == "access"   # 두 번째는 묻지 않음


@pytest.mark.parametrize("label,field,value", [("휠체어", "mobility", "wheelchair_manual"), ("오래 걷기 힘듦", "mobility", "senior"),
                                               ("유아차", "mobility", "stroller"), ("눈이 불편", "vision", "low"),
                                               ("귀가 불편", "hearing", "hard")])
def test_each_choice_keeps_question_and_adds_condition(orch, label, field, value):
    e = ask(orch, "서귀포 2일 코스 짜줘", {}).request.params["edit"]
    q = next(c["q"] for c in e["choices"] if c["label"] == label)
    req = ask(orch, q, {}).request                                      # 조건이 생겼으니 다시 묻지 않음
    assert req.intent.value == "course" and req.region == "서귀포시" and req.days == 2
    assert getattr(req.profile, field) == value


def test_no_condition_choice_runs_as_is(orch):
    e = ask(orch, "서귀포 2일 코스 짜줘", {}).request.params["edit"]
    q = e["choices"][-1]["q"]
    req = ask(orch, q, {}).request
    assert req.intent.value == "course" and req.profile.mobility == "none"


def test_not_asked_when_condition_given_or_not_needed(orch):
    for q in ("휠체어로 제주시 갈 만한 곳", "할머니랑 2일 코스", "보청기 쓰는데 갈 만한 곳", "읍면동별 무장애 관광지 현황"):
        assert not (ask(orch, q, {}).request.params.get("edit")), q
    assert not orch.plan("서귀포 2일 코스 짜줘", {}).request.params.get("edit")          # 기본은 묻지 않음(골든·폼)


def test_follow_up_and_edits_never_ask(orch):
    mem: dict = {}
    orch.ask("휠체어로 서귀포 2일 코스 짜줘", mem)
    mem.pop("profile_asked", None)
    assert ask(orch, "둘째 날 빡빡해", mem).request.params["edit"]["kind"] == "lighter"
    assert not (ask(orch, "다시 버스로", mem).request.params.get("edit"))
